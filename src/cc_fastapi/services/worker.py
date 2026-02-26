import logging
import threading
import time
from contextlib import suppress

from sqlalchemy.orm import Session

from cc_fastapi.core.config import get_settings
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

    def start(self) -> None:
        concurrency = max(1, self.settings.worker_concurrency)
        for idx in range(concurrency):
            worker_id = f"worker-{idx+1}"
            thread = threading.Thread(target=self._loop, args=(worker_id,), daemon=True)
            thread.start()
            self.threads.append(thread)
        logger.info("worker manager started", extra={"event_type": "worker_start", "worker_id": f"x{concurrency}"})

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

    def _loop(self, worker_id: str) -> None:
        sleep_seconds = max(0.2, self.settings.poll_interval_ms / 1000)
        while not self.stop_event.is_set():
            db = SessionLocal()
            try:
                self._maintenance(db)
                task = self.queue.claim_next_task(db, worker_id)
                if not task:
                    time.sleep(sleep_seconds)
                    continue
                logger.info("task running", extra={"task_id": task.id, "event_type": "running", "worker_id": worker_id})
                self._run_task(db, task.id)
            except Exception:
                logger.exception("worker loop error", extra={"event_type": "worker_loop_error", "worker_id": worker_id})
                with suppress(Exception):
                    db.rollback()
                time.sleep(sleep_seconds)
            finally:
                db.close()

    def _maintenance(self, db: Session) -> None:
        self.queue.abandon_expired_queued(db)
        self.queue.abandon_expired_running(db)

    def _run_task(self, db: Session, task_id: str) -> None:
        task = self.queue.get_task(db, task_id)
        if not task:
            return
        if task.status != TaskStatus.RUNNING:
            return
        try:
            prompt = str(task.payload.get("prompt", ""))
            model = str(task.payload.get("model", self.settings.anthropic_model))
            if not prompt.strip():
                raise RuntimeError("task prompt is empty")
            result = self.client.run_agent_task(
                prompt=prompt,
                model=model,
                metadata=task.metadata_json,
                agent_mode=task.agent_mode,
                unattended=task.unattended,
            )
            self.queue.mark_success(db, task.id, result)
            logger.info(
                "task succeeded",
                extra={
                    "task_id": task.id,
                    "event_type": "succeeded",
                    "trace_id": result.get("session_id", ""),
                },
            )
        except Exception as exc:
            self.queue.mark_retry_or_failed(db, task.id, str(exc))
            logger.exception("task failed", extra={"task_id": task.id, "event_type": "failed"})

