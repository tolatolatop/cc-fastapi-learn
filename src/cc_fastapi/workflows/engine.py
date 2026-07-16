import logging
from dataclasses import dataclass

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cc_fastapi.db.models import (
    AgentTask,
    AgentTaskRetryLink,
    TaskStatus,
    WorkflowCorrelation,
    WorkflowResourceLock,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowStepRun,
    WorkflowStepStatus,
    WorkflowTaskLink,
    utc_now,
)
from cc_fastapi.services.queue import TaskQueueService
from cc_fastapi.workflows.base import (
    WorkflowCorrelationSpec,
    WorkflowEvent,
    WorkflowPostResult,
    WorkflowRetryConflictError,
    WorkflowTaskOutcome,
)
from cc_fastapi.workflows.registry import WorkflowRegistry


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkflowExecution:
    run: WorkflowRun
    tasks: tuple[AgentTask, ...]


class WorkflowEngine:
    def __init__(self, registry: WorkflowRegistry, queue: TaskQueueService | None = None) -> None:
        self.registry = registry
        self.queue = queue or TaskQueueService()

    def _new_run(self, event: WorkflowEvent, workflow_name: str, workflow_version: str) -> WorkflowRun:
        return WorkflowRun(
            workflow_name=workflow_name,
            workflow_version=workflow_version,
            provider=event.provider,
            event_type=event.event_type,
            event_uuid=event.event_uuid,
            webhook_uuid=event.webhook_uuid,
            instance_url=event.instance_url,
            payload_json=event.payload,
            config_json=event.config,
            context_json={},
            status=WorkflowRunStatus.PLANNING,
        )

    @staticmethod
    def _begin_sqlite_write_transaction(db: Session) -> None:
        """Serialize SQLite workflow mutations before their first read."""
        if db.get_bind().dialect.name != "sqlite":
            return
        if db.in_transaction():
            db.rollback()
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")

    def _persist_failed_start(
        self,
        db: Session,
        event: WorkflowEvent,
        workflow_name: str,
        workflow_version: str,
        error: Exception,
    ) -> None:
        db.rollback()
        now = utc_now()
        run = self._new_run(event, workflow_name, workflow_version)
        run.status = WorkflowRunStatus.FAILED
        run.error_message = str(error)
        run.updated_at = now
        run.finished_at = now
        db.add(run)
        db.flush()
        db.add(
            WorkflowStepRun(
                workflow_run_id=run.id,
                step_name="before",
                status=WorkflowStepStatus.FAILED,
                input_json={"provider": event.provider, "event_type": event.event_type},
                error_message=str(error),
                finished_at=now,
            )
        )
        db.commit()

    @staticmethod
    def _correlation_dict(spec: WorkflowCorrelationSpec) -> dict[str, str]:
        return {
            "provider": spec.provider,
            "resource_type": spec.resource_type,
            "project_path": spec.project_path,
            "resource_id": spec.resource_id,
        }

    def _persist_correlations(
        self,
        db: Session,
        workflow_run_id: str,
        specs: tuple[WorkflowCorrelationSpec, ...],
    ) -> tuple[WorkflowCorrelationSpec, ...]:
        unique_specs = tuple(dict.fromkeys(specs))
        for spec in unique_specs:
            db.add(
                WorkflowCorrelation(
                    workflow_run_id=workflow_run_id,
                    provider=spec.provider,
                    resource_type=spec.resource_type,
                    project_path=spec.project_path,
                    resource_id=spec.resource_id,
                )
            )
        return unique_specs

    def _acquire_resource_locks(
        self,
        db: Session,
        specs: tuple[WorkflowCorrelationSpec, ...],
    ) -> None:
        """Lock resource keys in stable order until the caller commits the transaction."""
        unique_specs = sorted(
            set(specs),
            key=lambda spec: (spec.provider, spec.resource_type, spec.project_path, spec.resource_id),
        )
        for spec in unique_specs:
            filters = (
                WorkflowResourceLock.provider == spec.provider,
                WorkflowResourceLock.resource_type == spec.resource_type,
                WorkflowResourceLock.project_path == spec.project_path,
                WorkflowResourceLock.resource_id == spec.resource_id,
            )
            lock_row = db.scalar(select(WorkflowResourceLock).where(*filters).with_for_update())
            if lock_row is None:
                try:
                    with db.begin_nested():
                        db.add(
                            WorkflowResourceLock(
                                provider=spec.provider,
                                resource_type=spec.resource_type,
                                project_path=spec.project_path,
                                resource_id=spec.resource_id,
                            )
                        )
                        db.flush()
                except IntegrityError:
                    # A concurrent transaction created the lock row. Its unique
                    # key serialization is the lock acquisition for this path.
                    pass
                lock_row = db.scalar(
                    select(WorkflowResourceLock)
                    .where(*filters)
                    .execution_options(populate_existing=True)
                    .with_for_update()
                )
            if lock_row is None:
                raise RuntimeError(f"failed to acquire workflow resource lock: {self._correlation_dict(spec)}")

    def _supersede_correlated_runs(
        self,
        db: Session,
        current_run: WorkflowRun,
        specs: tuple[WorkflowCorrelationSpec, ...],
    ) -> list[dict[str, object]]:
        unique_specs = tuple(dict.fromkeys(specs))
        if not unique_specs:
            return []

        correlation_match = or_(
            *(
                and_(
                    WorkflowCorrelation.provider == spec.provider,
                    WorkflowCorrelation.resource_type == spec.resource_type,
                    WorkflowCorrelation.project_path == spec.project_path,
                    WorkflowCorrelation.resource_id == spec.resource_id,
                )
                for spec in unique_specs
            )
        )
        old_runs = list(
            db.scalars(
                select(WorkflowRun)
                .join(WorkflowCorrelation, WorkflowCorrelation.workflow_run_id == WorkflowRun.id)
                .where(
                    WorkflowRun.id != current_run.id,
                    correlation_match,
                    WorkflowRun.status.in_([WorkflowRunStatus.PLANNING, WorkflowRunStatus.RUNNING]),
                )
                .distinct()
                .order_by(WorkflowRun.created_at.asc())
                .with_for_update()
            )
        )

        superseded: list[dict[str, object]] = []
        now = utc_now()
        for old_run in old_runs:
            active_tasks = list(
                db.scalars(
                    select(AgentTask)
                    .join(WorkflowTaskLink, WorkflowTaskLink.task_id == AgentTask.id)
                    .where(
                        WorkflowTaskLink.workflow_run_id == old_run.id,
                        WorkflowTaskLink.is_active.is_(True),
                        AgentTask.status.in_([TaskStatus.QUEUED, TaskStatus.RUNNING]),
                    )
                    .order_by(WorkflowTaskLink.ordinal.asc())
                    .with_for_update()
                )
            )
            cancelled_task_ids: list[str] = []
            for task in active_tasks:
                cancelled = self.queue.cancel_task(
                    db,
                    task.id,
                    commit=False,
                    reason=f"superseded_by_workflow:{current_run.id}",
                )
                if cancelled is not None and cancelled.status == TaskStatus.CANCELLED:
                    cancelled_task_ids.append(cancelled.id)

            old_run.status = WorkflowRunStatus.SUPERSEDED
            old_run.context_json = {
                **old_run.context_json,
                "superseded_by_workflow_run_id": current_run.id,
            }
            old_run.updated_at = now
            old_run.finished_at = now
            db.add(
                WorkflowStepRun(
                    workflow_run_id=old_run.id,
                    step_name=f"superseded_by:{current_run.id}",
                    status=WorkflowStepStatus.SUCCEEDED,
                    input_json={"correlations": [self._correlation_dict(spec) for spec in unique_specs]},
                    output_json={
                        "superseded_by_workflow_run_id": current_run.id,
                        "cancelled_task_ids": cancelled_task_ids,
                    },
                    finished_at=now,
                )
            )
            superseded.append(
                {
                    "workflow_run_id": old_run.id,
                    "cancelled_task_ids": cancelled_task_ids,
                }
            )
        return superseded

    def start(self, db: Session, event: WorkflowEvent) -> WorkflowExecution:
        workflow = self.registry.resolve(event)
        run = self._new_run(event, workflow.name, workflow.version)
        db.add(run)
        db.flush()
        before_step = WorkflowStepRun(
            workflow_run_id=run.id,
            step_name="before",
            status=WorkflowStepStatus.RUNNING,
            input_json={"provider": event.provider, "event_type": event.event_type},
        )
        db.add(before_step)
        db.flush()

        try:
            plan = workflow.before(event)
            self._acquire_resource_locks(
                db,
                (*plan.correlations, *plan.supersede_correlations),
            )
            now = utc_now()
            run.context_json = plan.context
            run.updated_at = now
            correlations = self._persist_correlations(db, run.id, plan.correlations)
            superseded_runs = self._supersede_correlated_runs(db, run, plan.supersede_correlations)

            if plan.skip_reason is not None:
                run.status = WorkflowRunStatus.SKIPPED
                run.skip_reason = plan.skip_reason
                run.finished_at = now
                before_step.status = WorkflowStepStatus.SKIPPED
                before_output: dict[str, object] = {"decision": "skip", "reason": plan.skip_reason}
                if correlations:
                    before_output["correlations"] = [self._correlation_dict(spec) for spec in correlations]
                if superseded_runs:
                    before_output["superseded_runs"] = superseded_runs
                before_step.output_json = before_output
                before_step.finished_at = now
                db.flush()
                return WorkflowExecution(run=run, tasks=())

            if not plan.tasks:
                raise RuntimeError("workflow produced neither tasks nor a skip decision")

            tasks: list[AgentTask] = []
            for ordinal, spec in enumerate(plan.tasks):
                task = self.queue.create_task(
                    db,
                    prompt=spec.prompt,
                    model=spec.model,
                    queue_name=spec.queue_name,
                    metadata=spec.metadata,
                    priority=spec.priority,
                    agent_mode=spec.agent_mode,
                    unattended=spec.unattended,
                    max_attempts=spec.max_attempts,
                    claude_agent_options=spec.claude_agent_options,
                    commit=False,
                )
                tasks.append(task)
                db.add(
                    WorkflowTaskLink(
                        workflow_run_id=run.id,
                        task_id=task.id,
                        role=spec.role,
                        ordinal=ordinal,
                    )
                )

            run.status = WorkflowRunStatus.RUNNING
            before_step.status = WorkflowStepStatus.SUCCEEDED
            before_step.output_json = {
                "decision": "create_tasks",
                "task_count": len(tasks),
                "task_ids": [task.id for task in tasks],
            }
            if correlations:
                before_step.output_json["correlations"] = [
                    self._correlation_dict(spec) for spec in correlations
                ]
            if superseded_runs:
                before_step.output_json["superseded_runs"] = superseded_runs
            before_step.finished_at = now
            db.flush()
            return WorkflowExecution(run=run, tasks=tuple(tasks))
        except Exception as exc:
            self._persist_failed_start(db, event, workflow.name, workflow.version, exc)
            raise

    def get_run_for_trigger(self, db: Session, trigger_id: int) -> WorkflowRun | None:
        return db.scalar(select(WorkflowRun).where(WorkflowRun.webhook_trigger_id == trigger_id).limit(1))

    def handle_task_terminal(self, db: Session, task_id: str) -> list[WorkflowRun]:
        self._begin_sqlite_write_transaction(db)
        task = db.get(AgentTask, task_id)
        if task is None or task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
            db.rollback()
            return []

        links = list(
            db.scalars(
                select(WorkflowTaskLink).where(
                    WorkflowTaskLink.task_id == task_id,
                    WorkflowTaskLink.is_active.is_(True),
                )
            )
        )
        updated_runs: list[WorkflowRun] = []
        for link in links:
            run = db.scalar(
                select(WorkflowRun)
                .where(WorkflowRun.id == link.workflow_run_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            task = db.scalar(
                select(AgentTask)
                .where(AgentTask.id == task_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if run is None or run.status in {
                WorkflowRunStatus.SKIPPED,
                WorkflowRunStatus.SUCCEEDED,
                WorkflowRunStatus.FAILED,
                WorkflowRunStatus.SUPERSEDED,
            }:
                continue

            step_name = f"after_task:{task.id}"
            existing_step = db.scalar(
                select(WorkflowStepRun)
                .where(
                    WorkflowStepRun.workflow_run_id == run.id,
                    WorkflowStepRun.step_name == step_name,
                )
                .limit(1)
                .with_for_update()
            )
            if existing_step is not None:
                continue

            step = WorkflowStepRun(
                workflow_run_id=run.id,
                step_name=step_name,
                status=WorkflowStepStatus.RUNNING,
                input_json={"task_id": task.id, "task_status": task.status.value},
            )
            db.add(step)
            db.flush()

            workflow = self.registry.get(run.workflow_name, run.workflow_version)
            event = WorkflowEvent(
                provider=run.provider,
                event_type=run.event_type,
                payload=run.payload_json,
                event_uuid=run.event_uuid,
                webhook_uuid=run.webhook_uuid,
                instance_url=run.instance_url,
                config=run.config_json,
            )
            outcome = WorkflowTaskOutcome(
                task_id=task.id,
                status=task.status,
                result=task.result,
                error_message=task.error_message,
            )

            try:
                post_result = workflow.after_task(event, outcome, run.context_json)
                if not isinstance(post_result, WorkflowPostResult):
                    raise TypeError("workflow after_task must return WorkflowPostResult")
                run.context_json = {**run.context_json, **post_result.context_updates}
                step.status = WorkflowStepStatus.SUCCEEDED
                step.output_json = post_result.context_updates
                step.finished_at = utc_now()

                linked_tasks = list(
                    db.scalars(
                        select(AgentTask)
                        .join(WorkflowTaskLink, WorkflowTaskLink.task_id == AgentTask.id)
                        .where(
                            WorkflowTaskLink.workflow_run_id == run.id,
                            WorkflowTaskLink.is_active.is_(True),
                        )
                        .with_for_update()
                    )
                )
                active = any(item.status in {TaskStatus.QUEUED, TaskStatus.RUNNING} for item in linked_tasks)
                failed = any(item.status != TaskStatus.SUCCEEDED for item in linked_tasks if item.status not in {TaskStatus.QUEUED, TaskStatus.RUNNING})
                completed_step_names = set(
                    db.scalars(
                        select(WorkflowStepRun.step_name).where(
                            WorkflowStepRun.workflow_run_id == run.id,
                            WorkflowStepRun.step_name.like("after_task:%"),
                        ).with_for_update()
                    )
                )
                all_terminal_tasks_processed = all(
                    item.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}
                    or f"after_task:{item.id}" in completed_step_names
                    for item in linked_tasks
                )
                if not active and all_terminal_tasks_processed:
                    run.status = WorkflowRunStatus.FAILED if failed else WorkflowRunStatus.SUCCEEDED
                    run.finished_at = utc_now()
                run.updated_at = utc_now()
                updated_runs.append(run)
            except Exception as exc:
                now = utc_now()
                step.status = WorkflowStepStatus.FAILED
                step.error_message = str(exc)
                step.finished_at = now
                run.status = WorkflowRunStatus.FAILED
                run.error_message = str(exc)
                run.updated_at = now
                run.finished_at = now
                updated_runs.append(run)
                logger.exception(
                    "workflow after_task failed",
                    extra={"event_type": "workflow_after_task_failed", "task_id": task.id, "reason": str(exc)},
                )

        if updated_runs:
            db.commit()
        else:
            db.rollback()
        return updated_runs

    def reconcile_terminal_tasks(self, db: Session) -> int:
        task_ids = list(
            db.scalars(
                select(WorkflowTaskLink.task_id)
                .join(AgentTask, AgentTask.id == WorkflowTaskLink.task_id)
                .join(WorkflowRun, WorkflowRun.id == WorkflowTaskLink.workflow_run_id)
                .where(
                    WorkflowRun.status == WorkflowRunStatus.RUNNING,
                    WorkflowTaskLink.is_active.is_(True),
                    AgentTask.status.not_in([TaskStatus.QUEUED, TaskStatus.RUNNING]),
                )
                .distinct()
            )
        )
        updated = 0
        for task_id in task_ids:
            updated += len(self.handle_task_terminal(db, task_id))
        return updated

    def retry_task(self, db: Session, original_task_id: str) -> AgentTask | None:
        """Atomically claim and replace one task retry attempt."""
        self._begin_sqlite_write_transaction(db)
        try:
            original_link = db.scalar(
                select(WorkflowTaskLink)
                .where(WorkflowTaskLink.task_id == original_task_id)
                .limit(1)
            )
            if original_link is not None:
                run = db.scalar(
                    select(WorkflowRun)
                    .where(WorkflowRun.id == original_link.workflow_run_id)
                    .execution_options(populate_existing=True)
                    .with_for_update()
                )
                active_link = db.scalar(
                    select(WorkflowTaskLink)
                    .where(
                        WorkflowTaskLink.task_id == original_task_id,
                        WorkflowTaskLink.is_active.is_(True),
                    )
                    .execution_options(populate_existing=True)
                    .limit(1)
                    .with_for_update()
                )
                if (
                    run is None
                    or active_link is None
                    or run.status in {WorkflowRunStatus.SKIPPED, WorkflowRunStatus.SUPERSEDED}
                ):
                    raise WorkflowRetryConflictError("task retry was already replaced or workflow is not retryable")
            else:
                run = None
                active_link = None

            original_task = db.scalar(
                select(AgentTask)
                .where(AgentTask.id == original_task_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if original_task is None:
                db.rollback()
                return None
            existing_retry = db.scalar(
                select(AgentTaskRetryLink)
                .where(AgentTaskRetryLink.original_task_id == original_task_id)
                .with_for_update()
            )
            if existing_retry is not None:
                raise WorkflowRetryConflictError("task retry was already created")

            retried_task = self.queue.retry_task(db, original_task_id, commit=False)
            if retried_task is None:
                db.rollback()
                return None
            db.add(
                AgentTaskRetryLink(
                    original_task_id=original_task_id,
                    retried_task_id=retried_task.id,
                )
            )

            if run is not None and active_link is not None:
                active_link.is_active = False
                db.add(
                    WorkflowTaskLink(
                        workflow_run_id=run.id,
                        task_id=retried_task.id,
                        role=active_link.role,
                        ordinal=active_link.ordinal,
                        is_active=True,
                    )
                )
                now = utc_now()
                run.status = WorkflowRunStatus.RUNNING
                run.error_message = None
                run.finished_at = None
                run.updated_at = now
                db.add(
                    WorkflowStepRun(
                        workflow_run_id=run.id,
                        step_name=f"retry_task:{retried_task.id}",
                        status=WorkflowStepStatus.SUCCEEDED,
                        input_json={"original_task_id": original_task_id},
                        output_json={"retried_task_id": retried_task.id},
                        finished_at=now,
                    )
                )

            db.commit()
            db.refresh(retried_task)
            return retried_task
        except Exception:
            db.rollback()
            raise
