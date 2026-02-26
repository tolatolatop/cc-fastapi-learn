from datetime import timedelta
from typing import Any

from sqlalchemy import Select, func, select, update
from sqlalchemy.orm import Session

from cc_fastapi.core.config import get_settings
from cc_fastapi.db.models import AgentTask, AgentTaskLog, TaskStatus, utc_now


class TaskQueueService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def create_task(
        self,
        db: Session,
        *,
        prompt: str,
        model: str | None,
        metadata: dict[str, Any] | None,
        priority: int,
        agent_mode: bool,
        unattended: bool,
        max_attempts: int | None,
    ) -> AgentTask:
        now = utc_now()
        task = AgentTask(
            status=TaskStatus.QUEUED,
            priority=priority,
            payload={"prompt": prompt, "model": model or self.settings.anthropic_model},
            metadata_json=metadata or {},
            agent_mode=agent_mode,
            unattended=unattended,
            max_attempts=max_attempts or self.settings.max_attempts,
            queue_expire_at=now + timedelta(hours=self.settings.queue_ttl_hours),
            scheduled_at=now,
        )
        db.add(task)
        db.flush()
        self.log_event(db, task.id, "INFO", "queued", "task queued", {"priority": priority})
        db.commit()
        db.refresh(task)
        return task

    def _base_task_query(self) -> Select[tuple[AgentTask]]:
        return select(AgentTask)

    def get_task(self, db: Session, task_id: str) -> AgentTask | None:
        return db.get(AgentTask, task_id)

    def list_tasks(
        self, db: Session, status: TaskStatus | None, offset: int, limit: int
    ) -> tuple[list[AgentTask], int]:
        query = self._base_task_query()
        count_query = select(func.count()).select_from(AgentTask)
        if status:
            query = query.where(AgentTask.status == status)
            count_query = count_query.where(AgentTask.status == status)
        query = query.order_by(AgentTask.created_at.desc()).offset(offset).limit(limit)
        items = list(db.scalars(query))
        total = db.scalar(count_query) or 0
        return items, total

    def cancel_task(self, db: Session, task_id: str) -> AgentTask | None:
        task = self.get_task(db, task_id)
        if not task:
            return None
        if task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.ABANDONED}:
            return task
        task.status = TaskStatus.CANCELLED
        task.finished_at = utc_now()
        self.log_event(db, task.id, "INFO", "cancelled", "task cancelled by user", None)
        db.commit()
        db.refresh(task)
        return task

    def claim_next_task(self, db: Session, worker_id: str) -> AgentTask | None:
        candidate = db.scalar(
            select(AgentTask.id)
            .where(AgentTask.status == TaskStatus.QUEUED, AgentTask.queue_expire_at > utc_now())
            .order_by(AgentTask.priority.desc(), AgentTask.created_at.asc())
            .limit(1)
        )
        if not candidate:
            return None

        now = utc_now()
        result = db.execute(
            update(AgentTask)
            .where(AgentTask.id == candidate, AgentTask.status == TaskStatus.QUEUED)
            .values(
                status=TaskStatus.RUNNING,
                worker_id=worker_id,
                started_at=now,
                running_expire_at=now + timedelta(hours=self.settings.running_ttl_hours),
                attempt=AgentTask.attempt + 1,
            )
        )
        if result.rowcount == 0:
            db.rollback()
            return None

        task = self.get_task(db, candidate)
        if not task:
            db.rollback()
            return None
        self.log_event(db, task.id, "INFO", "running", "task claimed by worker", {"worker_id": worker_id})
        db.commit()
        db.refresh(task)
        return task

    def mark_success(self, db: Session, task_id: str, result_payload: dict[str, Any]) -> None:
        task = self.get_task(db, task_id)
        if not task or task.status != TaskStatus.RUNNING:
            return
        task.status = TaskStatus.SUCCEEDED
        task.result = result_payload
        task.finished_at = utc_now()
        self.log_event(db, task.id, "INFO", "succeeded", "task finished successfully", None)
        db.commit()

    def mark_retry_or_failed(self, db: Session, task_id: str, error_message: str) -> None:
        task = self.get_task(db, task_id)
        if not task or task.status != TaskStatus.RUNNING:
            return
        now = utc_now()
        can_retry = task.attempt < task.max_attempts
        if can_retry:
            task.status = TaskStatus.QUEUED
            task.error_message = error_message
            task.worker_id = None
            task.started_at = None
            task.running_expire_at = None
            task.queue_expire_at = now + timedelta(hours=self.settings.queue_ttl_hours)
            self.log_event(
                db,
                task.id,
                "WARNING",
                "retry",
                "task failed and re-queued",
                {"attempt": task.attempt, "max_attempts": task.max_attempts},
            )
        else:
            task.status = TaskStatus.FAILED
            task.error_message = error_message
            task.finished_at = now
            self.log_event(
                db,
                task.id,
                "ERROR",
                "failed",
                "task failed permanently",
                {"attempt": task.attempt, "max_attempts": task.max_attempts},
            )
        db.commit()

    def abandon_expired_queued(self, db: Session) -> int:
        now = utc_now()
        tasks = list(
            db.scalars(select(AgentTask).where(AgentTask.status == TaskStatus.QUEUED, AgentTask.queue_expire_at <= now))
        )
        for task in tasks:
            task.status = TaskStatus.ABANDONED
            task.abandoned_at = now
            task.abandoned_reason = "queue_timeout_24h"
            task.finished_at = now
            self.log_event(db, task.id, "WARNING", "abandoned", "task abandoned due to queue timeout", None)
        db.commit()
        return len(tasks)

    def abandon_expired_running(self, db: Session) -> int:
        now = utc_now()
        tasks = list(
            db.scalars(
                select(AgentTask).where(AgentTask.status == TaskStatus.RUNNING, AgentTask.running_expire_at <= now)
            )
        )
        for task in tasks:
            task.status = TaskStatus.ABANDONED
            task.abandoned_at = now
            task.abandoned_reason = "running_timeout_4h"
            task.error_message = "task_execution_timeout"
            task.finished_at = now
            self.log_event(db, task.id, "ERROR", "timeout", "task abandoned due to running timeout", None)
        db.commit()
        return len(tasks)

    def abandon_running_on_shutdown(self, db: Session) -> int:
        now = utc_now()
        tasks = list(db.scalars(select(AgentTask).where(AgentTask.status == TaskStatus.RUNNING)))
        for task in tasks:
            task.status = TaskStatus.ABANDONED
            task.abandoned_at = now
            task.abandoned_reason = "service_shutdown"
            task.finished_at = now
            self.log_event(db, task.id, "WARNING", "abandoned", "task abandoned due to service shutdown", None)
        db.commit()
        return len(tasks)

    def recover_orphan_running_on_startup(self, db: Session) -> int:
        now = utc_now()
        tasks = list(db.scalars(select(AgentTask).where(AgentTask.status == TaskStatus.RUNNING)))
        for task in tasks:
            task.status = TaskStatus.ABANDONED
            task.abandoned_at = now
            task.abandoned_reason = "startup_recovery"
            task.finished_at = now
            self.log_event(db, task.id, "WARNING", "abandoned", "task abandoned by startup recovery", None)
        db.commit()
        return len(tasks)

    def list_logs(self, db: Session, task_id: str, offset: int, limit: int) -> tuple[list[AgentTaskLog], int]:
        items = list(
            db.scalars(
                select(AgentTaskLog)
                .where(AgentTaskLog.task_id == task_id)
                .order_by(AgentTaskLog.ts.asc(), AgentTaskLog.id.asc())
                .offset(offset)
                .limit(limit)
            )
        )
        total = db.scalar(select(func.count()).select_from(AgentTaskLog).where(AgentTaskLog.task_id == task_id)) or 0
        return items, total

    def log_event(
        self,
        db: Session,
        task_id: str,
        level: str,
        event_type: str,
        message: str,
        metadata: dict[str, Any] | None,
    ) -> None:
        db.add(
            AgentTaskLog(
                task_id=task_id,
                level=level,
                event_type=event_type,
                message=message,
                metadata_json=metadata,
            )
        )

