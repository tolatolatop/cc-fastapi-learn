import logging
import threading
import time
from contextlib import suppress

from sqlalchemy.orm import Session

from cc_fastapi.core.config import get_settings
from cc_fastapi.core.queue_config import get_queue_config
from cc_fastapi.db.session import SessionLocal
from cc_fastapi.services.claude_client import ClaudeClient
from cc_fastapi.services.queue import TaskQueueService
from cc_fastapi.db.models import TaskStatus


logger = logging.getLogger(__name__)


class WorkerManager:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.queue = TaskQueueService()
        self.client = ClaudeClient()
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
            return self.queue.recover_orphan_running_on_startup(db)
        finally:
            db.close()

    def abandon_running_on_shutdown(self) -> int:
        db = SessionLocal()
        try:
            return self.queue.abandon_running_on_shutdown(db)
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
        if task.status != TaskStatus.RUNNING:
            return
        try:
            started_at = time.monotonic()
            prompt = str(task.payload.get("prompt", ""))
            model = str(task.payload.get("model", self.settings.anthropic_model))
            claude_agent_options = task.payload.get("claude_agent_options")
            if not isinstance(claude_agent_options, dict):
                claude_agent_options = {}
            if not prompt.strip():
                raise RuntimeError("task prompt is empty")
            result = self.client.run_agent_task(
                prompt=prompt,
                model=model,
                metadata=task.metadata_json,
                claude_agent_options=claude_agent_options,
                agent_mode=task.agent_mode,
                unattended=task.unattended,
                on_message_update=lambda messages: self.queue.upsert_task_context(db, task.id, messages),
            )
            self.queue.mark_success(db, task.id, result)
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
        except Exception as exc:
            self.queue.mark_retry_or_failed(db, task.id, str(exc))
            logger.exception(
                "task failed",
                extra={
                    "task_id": task.id,
                    "event_type": "failed",
                    "queue_name": getattr(task, "queue_name", "default"),
                },
            )

