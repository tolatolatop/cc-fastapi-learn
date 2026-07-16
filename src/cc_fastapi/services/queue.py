import logging
from copy import deepcopy
from datetime import timedelta
from typing import Any

from sqlalchemy import Select, func, select, update
from sqlalchemy.orm import Session

from cc_fastapi.core.config import get_settings
from cc_fastapi.core.queue_config import get_queue_config
from cc_fastapi.db.models import AgentTask, AgentTaskContext, AgentTaskLog, TaskStatus, utc_now

logger = logging.getLogger(__name__)


class QueueNotFoundError(ValueError):
    def __init__(self, queue_name: str) -> None:
        super().__init__(f"queue not found: {queue_name}")
        self.queue_name = queue_name


class TaskQueueService:
    def __init__(self) -> None:
        self.settings = get_settings()

    @staticmethod
    def _begin_sqlite_write_transaction(db: Session) -> None:
        if db.get_bind().dialect.name != "sqlite":
            return
        if db.in_transaction():
            db.rollback()
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")

    def create_task(
        self,
        db: Session,
        *,
        prompt: str,
        model: str | None,
        queue_name: str | None,
        metadata: dict[str, Any] | None,
        priority: int,
        agent_mode: bool,
        unattended: bool,
        max_attempts: int | None,
        claude_agent_options: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> AgentTask:
        now = utc_now()
        target_queue = self.resolve_target_queue(queue_name)
        task = AgentTask(
            status=TaskStatus.QUEUED,
            queue_name=target_queue,
            priority=priority,
            payload={
                "prompt": prompt,
                "model": model or self.settings.anthropic_model,
                "claude_agent_options": claude_agent_options or {},
            },
            metadata_json=metadata or {},
            agent_mode=agent_mode,
            unattended=unattended,
            max_attempts=max_attempts or self.settings.max_attempts,
            queue_expire_at=now + timedelta(hours=self.settings.queue_ttl_hours),
            scheduled_at=now,
        )
        db.add(task)
        db.flush()
        self.log_event(
            db, task.id, "INFO", "queued", "task queued", {"priority": priority, "queue_name": target_queue}
        )
        if commit:
            db.commit()
            db.refresh(task)
        return task

    def resolve_target_queue(self, queue_name: str | None) -> str:
        queue_cfg = get_queue_config()
        target = queue_name.strip() if isinstance(queue_name, str) and queue_name.strip() else queue_cfg.default_queue
        if target not in queue_cfg.queues:
            logger.warning(
                "queue resolve failed",
                extra={"event_type": "queue_resolve_failed", "queue_name": target},
            )
            raise QueueNotFoundError(target)
        logger.debug(
            "queue resolved",
            extra={
                "event_type": "queue_resolved",
                "queue_name": target,
                "reason": "explicit" if queue_name else "default_queue",
            },
        )
        return target

    def _base_task_query(self) -> Select[tuple[AgentTask]]:
        return select(AgentTask)

    def get_task(self, db: Session, task_id: str) -> AgentTask | None:
        return db.get(AgentTask, task_id)

    def is_task_cancelled(self, db: Session, task_id: str) -> bool:
        task = self.get_task(db, task_id)
        if task is None:
            return True
        db.refresh(task, attribute_names=["status"])
        return task.status == TaskStatus.CANCELLED

    def retry_task(self, db: Session, task_id: str, *, commit: bool = True) -> AgentTask | None:
        original_task = self.get_task(db, task_id)
        if original_task is None:
            return None

        payload = original_task.payload
        return self.create_task(
            db,
            prompt=payload["prompt"],
            model=payload.get("model"),
            queue_name=original_task.queue_name,
            metadata=deepcopy(original_task.metadata_json),
            priority=original_task.priority,
            agent_mode=original_task.agent_mode,
            unattended=original_task.unattended,
            max_attempts=original_task.max_attempts,
            claude_agent_options=deepcopy(payload.get("claude_agent_options")),
            commit=commit,
        )

    def get_task_context(self, db: Session, task_id: str) -> AgentTaskContext | None:
        return db.get(AgentTaskContext, task_id)

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

    def cancel_task(
        self,
        db: Session,
        task_id: str,
        *,
        commit: bool = True,
        reason: str = "user",
    ) -> AgentTask | None:
        now = utc_now()
        result = db.execute(
            update(AgentTask)
            .where(
                AgentTask.id == task_id,
                AgentTask.status.in_([TaskStatus.QUEUED, TaskStatus.RUNNING]),
            )
            .values(status=TaskStatus.CANCELLED, finished_at=now)
        )
        if result.rowcount == 0:
            if commit:
                db.rollback()
            return self.get_task(db, task_id)
        task = self.get_task(db, task_id)
        if task is None:
            raise RuntimeError(f"cancelled task disappeared before commit: {task_id}")
        normalized_reason = reason.strip() or "user"
        message = "task cancelled by user" if normalized_reason == "user" else "task cancelled by workflow"
        self.log_event(db, task.id, "INFO", "cancelled", message, {"reason": normalized_reason})
        if commit:
            db.commit()
            db.refresh(task)
        return task

    def claim_next_task(self, db: Session, worker_id: str, queue_name: str) -> AgentTask | None:
        candidate_query = (
            select(AgentTask.id)
            .where(
                AgentTask.status == TaskStatus.QUEUED,
                AgentTask.queue_expire_at > utc_now(),
                AgentTask.queue_name == queue_name,
            )
            .order_by(AgentTask.priority.desc(), AgentTask.created_at.asc())
            .limit(1)
        )
        candidate = db.scalar(candidate_query)
        if not candidate:
            db.rollback()
            return None
        if db.get_bind().dialect.name == "sqlite":
            self._begin_sqlite_write_transaction(db)
            candidate = db.scalar(candidate_query)
            if not candidate:
                db.rollback()
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
            logger.debug(
                "queue claim lost race",
                extra={"event_type": "queue_claim_race", "worker_id": worker_id, "queue_name": queue_name},
            )
            db.rollback()
            return None

        task = self.get_task(db, candidate)
        if not task:
            db.rollback()
            return None
        self.log_event(
            db,
            task.id,
            "INFO",
            "running",
            "task claimed by worker",
            {"worker_id": worker_id, "queue_name": queue_name},
        )
        db.commit()
        db.refresh(task)
        return task

    def mark_success(self, db: Session, task_id: str, result_payload: dict[str, Any]) -> bool:
        result = db.execute(
            update(AgentTask)
            .where(AgentTask.id == task_id, AgentTask.status == TaskStatus.RUNNING)
            .values(
                status=TaskStatus.SUCCEEDED,
                result=result_payload,
                finished_at=utc_now(),
            )
        )
        if result.rowcount == 0:
            db.rollback()
            return False
        self.log_event(db, task_id, "INFO", "succeeded", "task finished successfully", None)
        db.commit()
        logger.info(
            "task status transitioned",
            extra={
                "event_type": "status_transition",
                "task_id": task_id,
                "status_from": TaskStatus.RUNNING,
                "status_to": TaskStatus.SUCCEEDED,
            },
        )
        return True

    def mark_retry_or_failed(self, db: Session, task_id: str, error_message: str) -> bool:
        now = utc_now()
        retry_result = db.execute(
            update(AgentTask)
            .where(
                AgentTask.id == task_id,
                AgentTask.status == TaskStatus.RUNNING,
                AgentTask.attempt < AgentTask.max_attempts,
            )
            .values(
                status=TaskStatus.QUEUED,
                error_message=error_message,
                worker_id=None,
                started_at=None,
                running_expire_at=None,
                queue_expire_at=now + timedelta(hours=self.settings.queue_ttl_hours),
            )
        )
        if retry_result.rowcount:
            target_status = TaskStatus.QUEUED
        else:
            failed_result = db.execute(
                update(AgentTask)
                .where(
                    AgentTask.id == task_id,
                    AgentTask.status == TaskStatus.RUNNING,
                    AgentTask.attempt >= AgentTask.max_attempts,
                )
                .values(
                    status=TaskStatus.FAILED,
                    error_message=error_message,
                    finished_at=now,
                )
            )
            if failed_result.rowcount == 0:
                db.rollback()
                return False
            target_status = TaskStatus.FAILED

        task = self.get_task(db, task_id)
        if task is None:
            db.rollback()
            return False
        if target_status == TaskStatus.QUEUED:
            self.log_event(
                db,
                task.id,
                "WARNING",
                "retry",
                "task failed and re-queued",
                {"attempt": task.attempt, "max_attempts": task.max_attempts},
            )
            logger.info(
                "task status transitioned",
                extra={
                    "event_type": "status_transition",
                    "task_id": task_id,
                    "status_from": TaskStatus.RUNNING,
                    "status_to": target_status,
                },
            )
        else:
            self.log_event(
                db,
                task.id,
                "ERROR",
                "failed",
                "task failed permanently",
                {"attempt": task.attempt, "max_attempts": task.max_attempts},
            )
            logger.info(
                "task status transitioned",
                extra={
                    "event_type": "status_transition",
                    "task_id": task_id,
                    "status_from": TaskStatus.RUNNING,
                    "status_to": target_status,
                },
            )
        db.commit()
        return True

    def abandon_expired_queued(self, db: Session) -> int:
        now = utc_now()
        tasks_query = select(AgentTask).where(
            AgentTask.status == TaskStatus.QUEUED,
            AgentTask.queue_expire_at <= now,
        ).order_by(AgentTask.id)
        tasks = list(db.scalars(tasks_query))
        if not tasks:
            db.rollback()
            return 0
        if db.get_bind().dialect.name == "sqlite":
            self._begin_sqlite_write_transaction(db)
            tasks = list(db.scalars(tasks_query))
        changed: list[AgentTask] = []
        for task in tasks:
            result = db.execute(
                update(AgentTask)
                .where(
                    AgentTask.id == task.id,
                    AgentTask.status == TaskStatus.QUEUED,
                    AgentTask.queue_expire_at <= now,
                )
                .execution_options(synchronize_session=False)
                .values(
                    status=TaskStatus.ABANDONED,
                    abandoned_at=now,
                    abandoned_reason="queue_timeout_24h",
                    finished_at=now,
                )
            )
            if result.rowcount == 0:
                continue
            changed.append(task)
            self.log_event(db, task.id, "WARNING", "abandoned", "task abandoned due to queue timeout", None)
        db.commit()
        if changed:
            logger.warning(
                "abandoned queued timeout tasks",
                extra={"event_type": "queue_timeout_abandon", "reason": f"count={len(changed)}"},
            )
        return len(changed)

    def abandon_expired_running(self, db: Session) -> int:
        now = utc_now()
        tasks_query = select(AgentTask).where(
            AgentTask.status == TaskStatus.RUNNING,
            AgentTask.running_expire_at <= now,
        ).order_by(AgentTask.id)
        tasks = list(db.scalars(tasks_query))
        if not tasks:
            db.rollback()
            return 0
        if db.get_bind().dialect.name == "sqlite":
            self._begin_sqlite_write_transaction(db)
            tasks = list(db.scalars(tasks_query))
        changed: list[AgentTask] = []
        for task in tasks:
            result = db.execute(
                update(AgentTask)
                .where(
                    AgentTask.id == task.id,
                    AgentTask.status == TaskStatus.RUNNING,
                    AgentTask.running_expire_at <= now,
                )
                .execution_options(synchronize_session=False)
                .values(
                    status=TaskStatus.ABANDONED,
                    abandoned_at=now,
                    abandoned_reason="running_timeout_4h",
                    error_message="task_execution_timeout",
                    finished_at=now,
                )
            )
            if result.rowcount == 0:
                continue
            changed.append(task)
            self.log_event(db, task.id, "ERROR", "timeout", "task abandoned due to running timeout", None)
        db.commit()
        if changed:
            logger.warning(
                "abandoned running timeout tasks",
                extra={"event_type": "running_timeout_abandon", "reason": f"count={len(changed)}"},
            )
        return len(changed)

    def abandon_running_on_shutdown(self, db: Session) -> int:
        now = utc_now()
        tasks_query = select(AgentTask).where(AgentTask.status == TaskStatus.RUNNING).order_by(AgentTask.id)
        tasks = list(db.scalars(tasks_query))
        if not tasks:
            db.rollback()
            return 0
        if db.get_bind().dialect.name == "sqlite":
            self._begin_sqlite_write_transaction(db)
            tasks = list(db.scalars(tasks_query))
        changed: list[AgentTask] = []
        for task in tasks:
            result = db.execute(
                update(AgentTask)
                .where(AgentTask.id == task.id, AgentTask.status == TaskStatus.RUNNING)
                .execution_options(synchronize_session=False)
                .values(
                    status=TaskStatus.ABANDONED,
                    abandoned_at=now,
                    abandoned_reason="service_shutdown",
                    finished_at=now,
                )
            )
            if result.rowcount == 0:
                continue
            changed.append(task)
            self.log_event(db, task.id, "WARNING", "abandoned", "task abandoned due to service shutdown", None)
        db.commit()
        if changed:
            logger.warning(
                "abandoned running tasks on shutdown",
                extra={"event_type": "shutdown_abandon", "reason": f"count={len(changed)}"},
            )
        return len(changed)

    def recover_orphan_running_on_startup(self, db: Session) -> int:
        now = utc_now()
        tasks_query = select(AgentTask).where(AgentTask.status == TaskStatus.RUNNING).order_by(AgentTask.id)
        tasks = list(db.scalars(tasks_query))
        if not tasks:
            db.rollback()
            return 0
        if db.get_bind().dialect.name == "sqlite":
            self._begin_sqlite_write_transaction(db)
            tasks = list(db.scalars(tasks_query))
        changed: list[AgentTask] = []
        for task in tasks:
            result = db.execute(
                update(AgentTask)
                .where(AgentTask.id == task.id, AgentTask.status == TaskStatus.RUNNING)
                .execution_options(synchronize_session=False)
                .values(
                    status=TaskStatus.ABANDONED,
                    abandoned_at=now,
                    abandoned_reason="startup_recovery",
                    finished_at=now,
                )
            )
            if result.rowcount == 0:
                continue
            changed.append(task)
            self.log_event(db, task.id, "WARNING", "abandoned", "task abandoned by startup recovery", None)
        db.commit()
        if changed:
            logger.warning(
                "recovered orphan running tasks",
                extra={"event_type": "startup_recovery_abandon", "reason": f"count={len(changed)}"},
            )
        return len(changed)

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

    def upsert_task_context(self, db: Session, task_id: str, messages: list[str]) -> None:
        context = db.get(AgentTaskContext, task_id)
        now = utc_now()
        if context is None:
            db.add(
                AgentTaskContext(
                    task_id=task_id,
                    messages_json=messages,
                    updated_at=now,
                )
            )
        else:
            context.messages_json = messages
            context.updated_at = now
        db.commit()
