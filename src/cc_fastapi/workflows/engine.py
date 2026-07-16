import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from cc_fastapi.db.models import (
    AgentTask,
    TaskStatus,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowStepRun,
    WorkflowStepStatus,
    WorkflowTaskLink,
    utc_now,
)
from cc_fastapi.services.queue import TaskQueueService
from cc_fastapi.workflows.base import WorkflowEvent, WorkflowPostResult, WorkflowTaskOutcome
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
            now = utc_now()
            run.context_json = plan.context
            run.updated_at = now

            if plan.skip_reason is not None:
                run.status = WorkflowRunStatus.SKIPPED
                run.skip_reason = plan.skip_reason
                run.finished_at = now
                before_step.status = WorkflowStepStatus.SKIPPED
                before_step.output_json = {"decision": "skip", "reason": plan.skip_reason}
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
            before_step.finished_at = now
            db.flush()
            return WorkflowExecution(run=run, tasks=tuple(tasks))
        except Exception as exc:
            self._persist_failed_start(db, event, workflow.name, workflow.version, exc)
            raise

    def get_run_for_trigger(self, db: Session, trigger_id: int) -> WorkflowRun | None:
        return db.scalar(select(WorkflowRun).where(WorkflowRun.webhook_trigger_id == trigger_id).limit(1))

    def handle_task_terminal(self, db: Session, task_id: str) -> list[WorkflowRun]:
        task = db.get(AgentTask, task_id)
        if task is None or task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
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
            run = db.get(WorkflowRun, link.workflow_run_id)
            if run is None or run.status in {
                WorkflowRunStatus.SKIPPED,
                WorkflowRunStatus.SUCCEEDED,
                WorkflowRunStatus.FAILED,
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
                    )
                )
                active = any(item.status in {TaskStatus.QUEUED, TaskStatus.RUNNING} for item in linked_tasks)
                failed = any(item.status != TaskStatus.SUCCEEDED for item in linked_tasks if item.status not in {TaskStatus.QUEUED, TaskStatus.RUNNING})
                completed_step_names = set(
                    db.scalars(
                        select(WorkflowStepRun.step_name).where(
                            WorkflowStepRun.workflow_run_id == run.id,
                            WorkflowStepRun.step_name.like("after_task:%"),
                        )
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

    def handle_task_retry(self, db: Session, original_task_id: str, retried_task_id: str) -> list[WorkflowRun]:
        links = list(
            db.scalars(
                select(WorkflowTaskLink).where(
                    WorkflowTaskLink.task_id == original_task_id,
                    WorkflowTaskLink.is_active.is_(True),
                )
            )
        )
        updated_runs: list[WorkflowRun] = []
        for link in links:
            run = db.get(WorkflowRun, link.workflow_run_id)
            if run is None or run.status == WorkflowRunStatus.SKIPPED:
                continue
            link.is_active = False
            db.add(
                WorkflowTaskLink(
                    workflow_run_id=run.id,
                    task_id=retried_task_id,
                    role=link.role,
                    ordinal=link.ordinal,
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
                    step_name=f"retry_task:{retried_task_id}",
                    status=WorkflowStepStatus.SUCCEEDED,
                    input_json={"original_task_id": original_task_id},
                    output_json={"retried_task_id": retried_task_id},
                    finished_at=now,
                )
            )
            updated_runs.append(run)
        if updated_runs:
            db.commit()
        return updated_runs
