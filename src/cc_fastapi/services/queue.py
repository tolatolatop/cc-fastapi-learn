import logging
from copy import deepcopy
from datetime import UTC, timedelta
from math import ceil
from typing import Any
from uuid import uuid4

from sqlalchemy import Select, String, cast, func, or_, select, update
from sqlalchemy.orm import Session

from cc_fastapi.core.config import get_settings
from cc_fastapi.core.internal_tasks import REVIEW_ISSUE_IMPORT_QUEUE_NAME
from cc_fastapi.core.queue_config import get_queue_config
from cc_fastapi.db.models import (
    ACTIVE_TASK_STATUSES,
    CONTINUABLE_TASK_STATUSES,
    AgentTask,
    AgentTaskContext,
    AgentTaskLog,
    TaskQueueControl,
    TaskResumeMode,
    TaskStatus,
    utc_now,
)

logger = logging.getLogger(__name__)


class QueueNotFoundError(ValueError):
    def __init__(self, queue_name: str) -> None:
        super().__init__(f"queue not found: {queue_name}")
        self.queue_name = queue_name


class TaskControlConflict(ValueError):
    pass


class TaskQueueService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def _lock_control(self, db: Session) -> TaskQueueControl:
        """Serialize enqueue/claim/pause/resume, including across processes.

        The upsert acquires SQLite's write lock without rolling back a caller's
        workflow transaction. Row locking serves the same purpose on SQL servers.
        """
        dialect = db.get_bind().dialect.name
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
            statement = insert(TaskQueueControl).values(id=1, paused=False).on_conflict_do_nothing()
        elif dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
            statement = insert(TaskQueueControl).values(id=1, paused=False).on_conflict_do_nothing()
        else:
            from sqlalchemy.dialects.mysql import insert
            statement = insert(TaskQueueControl).values(id=1, paused=False).on_duplicate_key_update(id=1)
        db.execute(statement)
        return db.scalars(
            select(TaskQueueControl).where(TaskQueueControl.id == 1).with_for_update()
            .execution_options(populate_existing=True)
        ).one()

    def control_status(self, db: Session) -> dict[str, Any]:
        paused = bool(db.scalar(select(TaskQueueControl.paused).where(TaskQueueControl.id == 1)))
        counts = dict(db.execute(
            select(AgentTask.status, func.count()).where(
                AgentTask.status.in_([TaskStatus.PAUSING, TaskStatus.PAUSED]),
            ).group_by(AgentTask.status)
        ).all())
        pausing_count = counts.get(TaskStatus.PAUSING, 0)
        if paused:
            pausing_count += db.scalar(select(func.count()).select_from(AgentTask).where(
                self._stopping_terminal_tasks(),
            )) or 0
        return {
            "state": "pausing" if pausing_count else "paused" if paused else "active",
            "paused": paused,
            "pausing_count": pausing_count,
            "paused_count": counts.get(TaskStatus.PAUSED, 0),
        }

    def pause_all(self, db: Session) -> dict[str, Any]:
        control = self._lock_control(db)
        control.paused = True
        now = utc_now()
        tasks = list(db.scalars(select(AgentTask).where(
            AgentTask.status.in_([TaskStatus.QUEUED, TaskStatus.RUNNING]),
        ).order_by(AgentTask.id).with_for_update().execution_options(populate_existing=True)))
        for task in tasks:
            running = task.status == TaskStatus.RUNNING
            task.status = TaskStatus.PAUSING if running else TaskStatus.PAUSED
            task.paused_at = now
            if running:
                task.resume_pending = True
                if task.running_expire_at:
                    task.running_remaining_seconds = max(0, ceil(
                        (task.running_expire_at.replace(tzinfo=UTC) - now).total_seconds()
                    ))
            self.log_event(db, task.id, "INFO", task.status.value, "global pause requested", None)
        db.commit()
        return self.control_status(db)

    def resume_all(self, db: Session) -> dict[str, Any]:
        control = self._lock_control(db)
        if db.scalar(select(AgentTask.id).where(or_(
            AgentTask.status == TaskStatus.PAUSING, self._stopping_terminal_tasks(),
        )).limit(1)):
            db.rollback()
            raise TaskControlConflict("仍有任务暂停中，请等待停止完成后再恢复")
        tasks = list(db.scalars(select(AgentTask).where(
            AgentTask.status == TaskStatus.PAUSED,
        ).order_by(AgentTask.id).with_for_update().execution_options(populate_existing=True)))
        missing = [task.id for task in tasks if task.resume_pending and not task.session_id]
        if missing:
            db.rollback()
            raise TaskControlConflict(f"任务缺少可恢复的会话，保持暂停：{', '.join(missing)}")
        now = utc_now()
        for task in tasks:
            if task.paused_at:
                elapsed = now - task.paused_at.replace(tzinfo=UTC)
                task.queue_expire_at = task.queue_expire_at.replace(tzinfo=UTC) + elapsed
            # A running task's old enqueue deadline may have elapsed while it ran.
            if task.resume_pending:
                task.queue_expire_at = now + timedelta(hours=self.settings.queue_ttl_hours)
            task.status = TaskStatus.QUEUED
            task.paused_at = None
            task.worker_id = None
            task.error_message = None
            self.log_event(db, task.id, "INFO", "resumed", "global pause released", None)
        control.paused = False
        db.commit()
        return self.control_status(db)

    @staticmethod
    def _stopping_terminal_tasks():
        # Cancellation can win the state transition before the SDK has stopped.
        return AgentTask.status.in_([TaskStatus.CANCELLED, TaskStatus.ABANDONED]) & (
            AgentTask.execution_token.is_not(None)
        )

    def release_execution(self, db: Session, task_id: str, execution_token: str | None) -> None:
        db.rollback()
        db.execute(update(AgentTask).where(
            AgentTask.id == task_id, AgentTask.execution_token == execution_token,
            AgentTask.status.not_in(ACTIVE_TASK_STATUSES),
        ).values(execution_token=None))
        db.commit()

    def is_task_pausing(self, db: Session, task_id: str) -> bool:
        # End read transactions so PostgreSQL/MySQL polling sees new requests.
        db.rollback()
        return db.scalar(select(AgentTask.status).where(AgentTask.id == task_id)) == TaskStatus.PAUSING

    def mark_paused(
        self, db: Session, task_id: str, execution_token: str | None,
        *, error: str | None = None, not_started: bool = False, stopped: bool = True,
    ) -> bool:
        self._lock_control(db)
        task = db.scalars(select(AgentTask).where(
            AgentTask.id == task_id, AgentTask.execution_token == execution_token,
            AgentTask.status.in_([TaskStatus.PAUSING, TaskStatus.RUNNING]),
        ).with_for_update().execution_options(populate_existing=True)).first()
        if task is None:
            db.rollback()
            return False
        now = utc_now()
        if not task.paused_at:
            task.paused_at = now
            if task.running_expire_at:
                task.running_remaining_seconds = max(0, ceil(
                    (task.running_expire_at.replace(tzinfo=UTC) - now).total_seconds()
                ))
        if not_started and not task.session_id:
            task.resume_pending = False
            task.attempt = max(0, task.attempt - 1)
        task.status = TaskStatus.PAUSED if stopped else TaskStatus.PAUSING
        if stopped:
            task.worker_id = None
            task.execution_token = None
        else:
            task.resume_pending = True
        task.error_message = error
        self.log_event(db, task.id, "ERROR" if error else "INFO", task.status.value, error or "task paused", None)
        db.commit()
        return True

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
        queue_default_model = get_queue_config().queues[target_queue].default_model
        resolved_model = model.strip() if isinstance(model, str) and model.strip() else None
        if resolved_model is None and isinstance(queue_default_model, str) and queue_default_model.strip():
            resolved_model = queue_default_model.strip()
        if resolved_model is None:
            resolved_model = self.settings.anthropic_model
        control = self._lock_control(db)
        task = AgentTask(
            status=TaskStatus.PAUSED if control.paused else TaskStatus.QUEUED,
            paused_at=now if control.paused else None,
            queue_name=target_queue,
            priority=priority,
            payload={
                "prompt": prompt,
                "model": resolved_model,
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
            db, task.id, "INFO", task.status.value, "task enqueued",
            {"priority": priority, "queue_name": target_queue},
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
        return task.status in (TaskStatus.CANCELLED, TaskStatus.ABANDONED)

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

    def continue_task(self, db: Session, task_id: str) -> AgentTask | None:
        """Re-queue a finished task and resume it in the same agent session.

        The task row, its session id and its payload are preserved; only the
        execution bookkeeping is reset. A conditional update makes concurrent
        clicks single-flight.
        """
        self._begin_sqlite_write_transaction(db)
        control = self._lock_control(db)
        task = db.scalars(
            select(AgentTask)
            .where(AgentTask.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if task is None:
            db.rollback()
            return None
        if task.status not in CONTINUABLE_TASK_STATUSES:
            db.rollback()
            raise TaskControlConflict("只有已完成的任务可以继续执行")
        if not task.session_id:
            db.rollback()
            raise TaskControlConflict("该任务没有可续跑的会话，请使用重新执行")

        now = utc_now()
        paused = control.paused
        previous_status = task.status
        next_count = task.continuation_count + 1
        result = db.execute(
            update(AgentTask)
            .where(AgentTask.id == task_id, AgentTask.status == previous_status)
            .values(
                status=TaskStatus.PAUSED if paused else TaskStatus.QUEUED,
                paused_at=now if paused else None,
                resume_pending=True,
                resume_mode=TaskResumeMode.CONTINUE,
                continuation_count=AgentTask.continuation_count + 1,
                execution_token=None,
                worker_id=None,
                error_message=None,
                finished_at=None,
                started_at=None,
                running_expire_at=None,
                running_remaining_seconds=None,
                queue_expire_at=now + timedelta(hours=self.settings.queue_ttl_hours),
                scheduled_at=now,
            )
        )
        if result.rowcount == 0:
            db.rollback()
            raise TaskControlConflict("任务状态已变化，请刷新后重试")
        self.log_event(
            db,
            task_id,
            "INFO",
            "continued",
            "task continued in the same session",
            {
                "previous_status": previous_status.value,
                "continuation_count": next_count,
                "session_id": task.session_id,
                "queue_name": task.queue_name,
            },
        )
        db.commit()
        db.refresh(task)
        logger.info(
            "task status transitioned",
            extra={
                "event_type": "status_transition",
                "task_id": task_id,
                "status_from": previous_status.value,
                "status_to": task.status.value,
                "reason": "continue",
            },
        )
        return task

    def set_task_session_id(
        self, db: Session, task_id: str, session_id: str, *, execution_token: str | None = None,
    ) -> None:
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            return
        if execution_token:
            self._lock_control(db)
        task = db.get(AgentTask, task_id, populate_existing=True)
        if execution_token and (task is None or task.execution_token != execution_token
                                or task.status not in (TaskStatus.RUNNING, TaskStatus.PAUSING)):
            db.rollback()
            return
        if task is None or task.session_id == normalized_session_id:
            if execution_token:
                db.rollback()
            return
        task.session_id = normalized_session_id
        self.log_event(
            db,
            task.id,
            "INFO",
            "session_started",
            "agent session started",
            {"session_id": normalized_session_id, "attempt": task.attempt},
        )
        db.commit()

    def list_tasks(
        self,
        db: Session,
        statuses: list[TaskStatus] | None,
        queue_name: str | None,
        search: str | None,
        offset: int,
        limit: int,
    ) -> tuple[list[AgentTask], int]:
        query = self._base_task_query()
        count_query = select(func.count()).select_from(AgentTask)
        filters = [AgentTask.queue_name != REVIEW_ISSUE_IMPORT_QUEUE_NAME]
        if statuses:
            filters.append(AgentTask.status.in_(statuses))
        if queue_name:
            filters.append(AgentTask.queue_name == queue_name)
        if search and (normalized_search := search.strip()):
            pattern = f"%{normalized_search}%"
            filters.append(
                or_(
                    AgentTask.id.ilike(pattern),
                    AgentTask.queue_name.ilike(pattern),
                    cast(AgentTask.payload, String).ilike(pattern),
                )
            )
        if filters:
            query = query.where(*filters)
            count_query = count_query.where(*filters)
        query = query.order_by(AgentTask.created_at.desc()).offset(offset).limit(limit)
        items = list(db.scalars(query))
        total = db.scalar(count_query) or 0
        return items, total

    def summarize_tasks(
        self, db: Session
    ) -> tuple[int, dict[TaskStatus, int], dict[str, dict[str, int]]]:
        status_counts = {status: 0 for status in TaskStatus}
        queue_counts: dict[str, dict[str, int]] = {}
        rows = db.execute(
            select(AgentTask.queue_name, AgentTask.status, func.count())
            .where(AgentTask.queue_name != REVIEW_ISSUE_IMPORT_QUEUE_NAME)
            .group_by(AgentTask.queue_name, AgentTask.status)
        )
        for queue_name, status, count in rows:
            normalized_count = int(count)
            status_counts[status] += normalized_count
            queue_summary = queue_counts.setdefault(queue_name, {"total": 0, "queued": 0, "running": 0})
            queue_summary["total"] += normalized_count
            if status == TaskStatus.QUEUED:
                queue_summary["queued"] = normalized_count
            elif status == TaskStatus.RUNNING:
                queue_summary["running"] = normalized_count
        return sum(status_counts.values()), status_counts, queue_counts

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
                AgentTask.status.in_(ACTIVE_TASK_STATUSES),
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
        if self._lock_control(db).paused:
            db.rollback()
            return None
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
        candidate_task = db.get(AgentTask, candidate, populate_existing=True)
        now = utc_now()
        resuming = candidate_task.resume_pending
        running_seconds = (
            candidate_task.running_remaining_seconds
            if resuming and candidate_task.running_remaining_seconds is not None
            else self.settings.running_ttl_hours * 3600
        )
        result = db.execute(
            update(AgentTask)
            .where(AgentTask.id == candidate, AgentTask.status == TaskStatus.QUEUED)
            .values(
                status=TaskStatus.RUNNING,
                worker_id=worker_id,
                # A continued task starts with a cleared started_at and gets a
                # fresh timestamp; a pause resume keeps the original one.
                started_at=candidate_task.started_at if resuming and candidate_task.started_at else now,
                running_expire_at=now + timedelta(seconds=running_seconds),
                attempt=AgentTask.attempt if resuming else AgentTask.attempt + 1,
                execution_token=str(uuid4()),
                session_id=candidate_task.session_id if resuming else None,
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

    def mark_success(
        self, db: Session, task_id: str, result_payload: dict[str, Any],
        *, execution_token: str | None = None,
    ) -> bool:
        values: dict[str, Any] = {
            "status": TaskStatus.SUCCEEDED,
            "result": result_payload,
            "finished_at": utc_now(),
            "resume_pending": False,
            "resume_mode": None,
        }
        result_session_id = result_payload.get("session_id")
        if isinstance(result_session_id, str) and result_session_id.strip():
            values["session_id"] = result_session_id.strip()
        result = db.execute(
            update(AgentTask)
            .where(
                AgentTask.id == task_id,
                AgentTask.status.in_([TaskStatus.RUNNING, TaskStatus.PAUSING]),
                *([AgentTask.execution_token == execution_token] if execution_token else []),
            )
            .values(**values)
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

    def mark_retry_or_failed(
        self,
        db: Session,
        task_id: str,
        error_message: str,
        error_metadata: dict[str, Any] | None = None,
        *, execution_token: str | None = None,
    ) -> bool:
        control = self._lock_control(db)
        now = utc_now()
        retry_result = db.execute(
            update(AgentTask)
            .where(
                AgentTask.id == task_id,
                AgentTask.status == TaskStatus.RUNNING,
                AgentTask.attempt < AgentTask.max_attempts,
                *([AgentTask.execution_token == execution_token] if execution_token else []),
            )
            .values(
                status=TaskStatus.PAUSED if control.paused else TaskStatus.QUEUED,
                paused_at=now if control.paused else None,
                resume_pending=False,
                resume_mode=None,
                execution_token=None,
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
                    *([AgentTask.execution_token == execution_token] if execution_token else []),
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
        diagnostic_metadata = {
            "attempt": task.attempt,
            "max_attempts": task.max_attempts,
            **(error_metadata or {}),
        }
        self.log_event(
            db,
            task.id,
            "ERROR",
            "execution_error",
            error_message,
            diagnostic_metadata,
        )
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

    def mark_continuation_failed(
        self,
        db: Session,
        task_id: str,
        error_message: str,
        *,
        execution_token: str | None = None,
    ) -> bool:
        """Fail one manual continuation without falling back to the pause state.

        A continuation is a manual action: it must not be parked as paused, and
        it must not silently start a brand new session. The session id stays on
        the row for audit; running this task again requires an explicit retry.
        """
        result = db.execute(
            update(AgentTask)
            .where(
                AgentTask.id == task_id,
                AgentTask.status == TaskStatus.RUNNING,
                *([AgentTask.execution_token == execution_token] if execution_token else []),
            )
            .values(
                status=TaskStatus.FAILED,
                error_message=error_message,
                finished_at=utc_now(),
                resume_pending=False,
                resume_mode=None,
                execution_token=None,
                worker_id=None,
                running_expire_at=None,
            )
        )
        if result.rowcount == 0:
            db.rollback()
            return False
        self.log_event(
            db,
            task_id,
            "ERROR",
            "failed",
            "continuation failed",
            {"continuation": True},
        )
        db.commit()
        logger.info(
            "task status transitioned",
            extra={
                "event_type": "status_transition",
                "task_id": task_id,
                "status_from": TaskStatus.RUNNING,
                "status_to": TaskStatus.FAILED,
                "reason": "continuation_failed",
            },
        )
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
        # A restart in maintenance mode must preserve pending pause requests.
        # This recovery is for the existing single application/worker process.
        control = self._lock_control(db)
        paused = control.paused
        db.execute(update(AgentTask).where(self._stopping_terminal_tasks()).values(execution_token=None))
        recover_statuses = [TaskStatus.PAUSING]
        if paused:
            recover_statuses.append(TaskStatus.RUNNING)
        interrupted_tasks = list(db.scalars(select(AgentTask).where(
            AgentTask.status.in_(recover_statuses),
        ).with_for_update()))
        for task in interrupted_tasks:
            task.status = TaskStatus.PAUSED
            task.paused_at = task.paused_at or utc_now()
            task.resume_pending = True
            task.execution_token = None
            task.worker_id = None
            self.log_event(db, task.id, "INFO", "paused", "pause preserved after restart", None)
        db.commit()
        recovered_count = len(interrupted_tasks)
        if paused:
            return recovered_count
        now = utc_now()
        tasks_query = select(AgentTask).where(AgentTask.status == TaskStatus.RUNNING).order_by(AgentTask.id)
        tasks = list(db.scalars(tasks_query))
        if not tasks:
            db.rollback()
            return recovered_count
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
        return recovered_count + len(changed)

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

    def upsert_task_context(
        self, db: Session, task_id: str, messages: list[str], *, execution_token: str | None = None,
    ) -> None:
        if execution_token:
            self._lock_control(db)
            task = db.get(AgentTask, task_id, populate_existing=True)
            if (task is None or task.execution_token != execution_token
                    or task.status not in (TaskStatus.RUNNING, TaskStatus.PAUSING)):
                db.rollback()
                return
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
