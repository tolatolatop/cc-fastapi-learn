import logging
import threading
import time
from contextlib import suppress

from sqlalchemy.orm import Session

from cc_fastapi.core.config import get_settings
from cc_fastapi.core.queue_config import get_queue_config
from cc_fastapi.db.models import TaskResumeMode, TaskStatus
from cc_fastapi.db.session import SessionLocal
from cc_fastapi.services.claude_client import (
    AgentTaskCancelledError,
    AgentTaskPausedError,
    AgentTaskStopError,
    ClaudeClient,
)
from cc_fastapi.services.queue import TaskQueueService
from cc_fastapi.workflows import build_default_workflow_engine

logger = logging.getLogger(__name__)


class WorkerManager:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.queue = TaskQueueService()
        self.client = ClaudeClient()
        self.workflows = build_default_workflow_engine()
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self._empty_log_last_ts: dict[str, float] = {}
        self._empty_log_interval_seconds: float = 30.0

    def start(self) -> None:
        queue_cfg = get_queue_config()
        started = 0
        logger.info(
            "worker manager queue config loaded",
            extra={
                "event_type": "worker_queue_config",
                "trace_id": queue_cfg.default_queue,
                "reason": ",".join(f"{name}:{cfg.workers}" for name, cfg in queue_cfg.queues.items()),
            },
        )
        for queue_name, qdef in queue_cfg.queues.items():
            workers = max(1, int(qdef.workers))
            for idx in range(workers):
                worker_id = f"{queue_name}-worker-{idx+1}"
                thread = threading.Thread(target=self._loop, args=(worker_id, queue_name), daemon=True)
                thread.start()
                self.threads.append(thread)
                started += 1
        logger.info(
            "worker manager started",
            extra={"event_type": "worker_start", "worker_id": f"x{started}", "trace_id": queue_cfg.default_queue},
        )

    def stop(self) -> None:
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=3)
        logger.info("worker manager stopped", extra={"event_type": "worker_stop"})

    def run_startup_recovery(self) -> int:
        db = SessionLocal()
        try:
            recovered = self.queue.recover_orphan_running_on_startup(db)
            self.workflows.reconcile_terminal_tasks(db)
            return recovered
        finally:
            db.close()

    def abandon_running_on_shutdown(self) -> int:
        db = SessionLocal()
        try:
            abandoned = self.queue.abandon_running_on_shutdown(db)
            self.workflows.reconcile_terminal_tasks(db)
            return abandoned
        finally:
            db.close()

    def _loop(self, worker_id: str, queue_name: str) -> None:
        sleep_seconds = max(0.2, self.settings.poll_interval_ms / 1000)
        logger.debug(
            "worker loop started",
            extra={"event_type": "worker_loop_start", "worker_id": worker_id, "queue_name": queue_name},
        )
        while not self.stop_event.is_set():
            db = SessionLocal()
            try:
                self._maintenance(db)
                task = self.queue.claim_next_task(db, worker_id, queue_name)
                if not task:
                    if self._should_log_empty_poll(worker_id):
                        logger.debug(
                            "worker poll no task",
                            extra={"event_type": "worker_poll_empty", "worker_id": worker_id, "queue_name": queue_name},
                        )
                    time.sleep(sleep_seconds)
                    continue
                logger.info(
                    "task running",
                    extra={"task_id": task.id, "event_type": "running", "worker_id": worker_id, "trace_id": queue_name},
                )
                self._run_task(db, task.id)
            except Exception:
                logger.exception(
                    "worker loop error",
                    extra={"event_type": "worker_loop_error", "worker_id": worker_id, "trace_id": queue_name},
                )
                with suppress(Exception):
                    db.rollback()
                time.sleep(sleep_seconds)
            finally:
                db.close()

    def _should_log_empty_poll(self, worker_id: str) -> bool:
        now = time.monotonic()
        last_ts = self._empty_log_last_ts.get(worker_id, 0.0)
        if now - last_ts < self._empty_log_interval_seconds:
            return False
        self._empty_log_last_ts[worker_id] = now
        return True

    def _maintenance(self, db: Session) -> None:
        queued_count = self.queue.abandon_expired_queued(db)
        running_count = self.queue.abandon_expired_running(db)
        self.workflows.reconcile_terminal_tasks(db)
        if queued_count or running_count:
            logger.warning(
                "worker maintenance abandoned tasks",
                extra={
                    "event_type": "worker_maintenance_abandon",
                    "reason": f"queued={queued_count},running={running_count}",
                },
            )

    def _run_task(self, db: Session, task_id: str) -> None:
        task = self.queue.get_task(db, task_id)
        if not task:
            return
        execution_token = task.execution_token
        if task.status == TaskStatus.PAUSING:
            self.queue.mark_paused(db, task_id, execution_token, not_started=True)
            return
        if task.status != TaskStatus.RUNNING:
            self.queue.release_execution(db, task_id, execution_token)
            return
        resuming = task.resume_pending
        continuing = resuming and task.resume_mode == TaskResumeMode.CONTINUE
        previous_context = self.queue.get_task_context(db, task_id) if resuming else None
        previous_messages = list(previous_context.messages_json) if previous_context else []
        stopped = True
        try:
            started_at = time.monotonic()
            prompt = str(task.payload.get("prompt", ""))
            model = str(task.payload.get("model", self.settings.anthropic_model))
            claude_agent_options = task.payload.get("claude_agent_options")
            if not isinstance(claude_agent_options, dict):
                claude_agent_options = {}
            if not prompt.strip():
                raise RuntimeError("task prompt is empty")
            if resuming and not task.session_id:
                raise RuntimeError("任务缺少可恢复的会话，保持暂停")
            result = self.client.run_agent_task(
                prompt=prompt,
                model=model,
                metadata=task.metadata_json,
                claude_agent_options=claude_agent_options,
                agent_mode=task.agent_mode,
                unattended=task.unattended,
                on_message_update=lambda messages: self.queue.upsert_task_context(
                    db, task_id, previous_messages + messages, execution_token=execution_token,
                ),
                on_session_id=lambda session_id: self.queue.set_task_session_id(
                    db, task_id, session_id, execution_token=execution_token,
                ),
                should_cancel=lambda: self.queue.is_task_cancelled(db, task_id),
                should_pause=lambda: self.queue.is_task_pausing(db, task_id),
                resume_session_id=task.session_id if resuming else None,
                resume_reason="continue" if continuing else ("pause" if resuming else None),
            )
            transitioned = self.queue.mark_success(db, task_id, result, execution_token=execution_token)
            self.workflows.handle_task_terminal(db, task.id)
            if not transitioned:
                logger.info(
                    "task result ignored after concurrent terminal transition",
                    extra={
                        "task_id": task.id,
                        "event_type": "terminal_transition_lost",
                        "queue_name": getattr(task, "queue_name", "default"),
                    },
                )
                return
            logger.info(
                "task succeeded",
                extra={
                    "task_id": task.id,
                    "event_type": "succeeded",
                    "trace_id": result.get("session_id", ""),
                    "queue_name": getattr(task, "queue_name", "default"),
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                },
            )
        except AgentTaskStopError as exc:
            stopped = False
            db.rollback()
            self.queue.mark_paused(db, task_id, execution_token, error=str(exc), stopped=False)
        except AgentTaskPausedError:
            db.rollback()
            self.queue.mark_paused(db, task_id, execution_token)
        except AgentTaskCancelledError:
            db.rollback()
            self.workflows.handle_task_terminal(db, task.id)
            logger.info(
                "task execution stopped after cancellation",
                extra={
                    "task_id": task.id,
                    "event_type": "cancelled",
                    "queue_name": getattr(task, "queue_name", "default"),
                },
            )
        except Exception as exc:
            db.rollback()
            current = self.queue.get_task(db, task_id)
            if (current is not None and current.status == TaskStatus.PAUSING
                    and current.execution_token == execution_token):
                # A failed interrupt/cleanup is not a confirmed pause.
                current.error_message = str(exc)
                db.commit()
                logger.exception("task pause did not complete", extra={"task_id": task_id})
                return
            if continuing:
                self.queue.mark_continuation_failed(
                    db, task_id, str(exc), execution_token=execution_token,
                )
                self.workflows.handle_task_terminal(db, task_id)
                logger.exception(
                    "task continuation failed",
                    extra={
                        "task_id": task_id,
                        "event_type": "continuation_failed",
                        "queue_name": getattr(task, "queue_name", "default"),
                    },
                )
                return
            if resuming:
                self.queue.mark_paused(db, task_id, execution_token, error=str(exc))
                return
            error_metadata = {
                "exception_type": getattr(exc, "error_type", type(exc).__name__),
                "exit_code": getattr(exc, "exit_code", None),
                "has_cli_stderr": bool(getattr(exc, "cli_stderr", "")),
            }
            self.queue.mark_retry_or_failed(
                db, task_id, str(exc), error_metadata, execution_token=execution_token,
            )
            terminal_task = self.queue.get_task(db, task.id)
            if terminal_task is not None and terminal_task.status == TaskStatus.FAILED:
                self.workflows.handle_task_terminal(db, task.id)
            logger.exception(
                "task failed",
                extra={
                    "task_id": task.id,
                    "event_type": "failed",
                    "queue_name": getattr(task, "queue_name", "default"),
                },
            )
        finally:
            if stopped:
                self.queue.release_execution(db, task_id, execution_token)
