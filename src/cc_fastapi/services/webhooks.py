from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cc_fastapi.db.models import (
    AgentTask,
    TaskStatus,
    WebhookDeduplicationKey,
    WebhookTrigger,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowStepRun,
    WorkflowStepStatus,
    WorkflowTaskLink,
    utc_now,
)
from cc_fastapi.workflows import WorkflowEngine, build_default_workflow_engine
from cc_fastapi.workflows.base import WorkflowEvent, WorkflowTemplateError


WebhookTemplateError = WorkflowTemplateError


class WebhookService:
    def __init__(self, workflows: WorkflowEngine | None = None) -> None:
        self.workflows = workflows or build_default_workflow_engine()

    def _adopt_legacy_trigger(
        self,
        db: Session,
        trigger: WebhookTrigger,
        task: AgentTask | None,
        *,
        prompt_template_path: str,
        queue_name: str | None,
    ) -> WorkflowRun:
        now = utc_now()
        if task is None:
            run_status = WorkflowRunStatus.SKIPPED
        elif task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
            run_status = WorkflowRunStatus.RUNNING
        elif task.status == TaskStatus.SUCCEEDED:
            run_status = WorkflowRunStatus.SUCCEEDED
        else:
            run_status = WorkflowRunStatus.FAILED

        run = WorkflowRun(
            workflow_name="gitlab_prompt_task",
            workflow_version="1",
            provider=trigger.provider,
            event_type=trigger.event_type,
            event_uuid=trigger.event_uuid,
            webhook_uuid=trigger.webhook_uuid,
            instance_url=trigger.instance_url,
            payload_json=trigger.payload_json,
            config_json={"prompt_template_path": prompt_template_path, "queue_name": queue_name},
            context_json={"legacy_adopted": True},
            status=run_status,
            skip_reason="legacy_trigger_without_task" if task is None else None,
            webhook_trigger_id=trigger.id,
            updated_at=now,
            finished_at=now if run_status not in {WorkflowRunStatus.PLANNING, WorkflowRunStatus.RUNNING} else None,
        )
        db.add(run)
        db.flush()
        db.add(
            WorkflowStepRun(
                workflow_run_id=run.id,
                step_name="before",
                status=WorkflowStepStatus.SUCCEEDED,
                output_json={"decision": "legacy_adopted", "task_id": task.id if task else None},
                finished_at=now,
            )
        )
        if task is not None:
            db.add(WorkflowTaskLink(workflow_run_id=run.id, task_id=task.id, role="primary", ordinal=0))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing_run = self.workflows.get_run_for_trigger(db, trigger.id)
            if existing_run is None:
                raise
            return existing_run
        db.refresh(run)
        return run

    def _find_existing_trigger(
        self,
        db: Session,
        *,
        provider: str,
        webhook_uuid: str | None,
    ) -> tuple[WebhookTrigger, AgentTask | None, WorkflowRun | None] | None:
        if webhook_uuid is None:
            return None
        trigger = db.scalar(
            select(WebhookTrigger)
            .where(
                WebhookTrigger.provider == provider,
                WebhookTrigger.webhook_uuid == webhook_uuid,
            )
            .order_by(WebhookTrigger.id.asc())
            .limit(1)
        )
        if trigger is None:
            return None
        task = db.get(AgentTask, trigger.task_id) if trigger.task_id else None
        if trigger.task_id is not None and task is None:
            raise RuntimeError(f"webhook trigger references missing task: {trigger.task_id}")
        workflow_run = self.workflows.get_run_for_trigger(db, trigger.id)
        return trigger, task, workflow_run

    def trigger_gitlab_task(
        self,
        db: Session,
        *,
        payload: dict[str, Any],
        event_type: str,
        event_uuid: str | None,
        webhook_uuid: str | None,
        instance_url: str | None,
        prompt_template_path: str,
        queue_name: str | None,
    ) -> tuple[WebhookTrigger, AgentTask | None, bool, WorkflowRun]:
        provider = "gitlab"
        webhook_uuid = webhook_uuid.strip() if webhook_uuid and webhook_uuid.strip() else None
        existing = self._find_existing_trigger(
            db,
            provider=provider,
            webhook_uuid=webhook_uuid,
        )
        if existing is not None:
            trigger, task, workflow_run = existing
            if workflow_run is None:
                workflow_run = self._adopt_legacy_trigger(
                    db,
                    trigger,
                    task,
                    prompt_template_path=prompt_template_path,
                    queue_name=queue_name,
                )
            return trigger, task, True, workflow_run

        execution = self.workflows.start(
            db,
            WorkflowEvent(
                provider=provider,
                event_type=event_type,
                payload=payload,
                event_uuid=event_uuid,
                webhook_uuid=webhook_uuid,
                instance_url=instance_url,
                config={
                    "prompt_template_path": prompt_template_path,
                    "queue_name": queue_name,
                },
            ),
        )
        task = execution.tasks[0] if execution.tasks else None
        trigger = WebhookTrigger(
            provider=provider,
            event_type=event_type,
            event_uuid=event_uuid,
            webhook_uuid=webhook_uuid,
            instance_url=instance_url,
            task_id=task.id if task else None,
            payload_json=payload,
        )
        db.add(trigger)
        db.flush()
        execution.run.webhook_trigger_id = trigger.id
        if webhook_uuid is not None:
            db.add(
                WebhookDeduplicationKey(
                    provider=provider,
                    webhook_uuid=webhook_uuid,
                    webhook_trigger_id=trigger.id,
                )
            )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = self._find_existing_trigger(
                db,
                provider=provider,
                webhook_uuid=webhook_uuid,
            )
            if existing is None:
                raise
            existing_trigger, existing_task, existing_run = existing
            if existing_run is None:
                existing_run = self._adopt_legacy_trigger(
                    db,
                    existing_trigger,
                    existing_task,
                    prompt_template_path=prompt_template_path,
                    queue_name=queue_name,
                )
            return existing_trigger, existing_task, True, existing_run
        if task is not None:
            db.refresh(task)
        db.refresh(trigger)
        db.refresh(execution.run)
        return trigger, task, False, execution.run

    def list_triggers(self, db: Session, offset: int, limit: int) -> tuple[list[WebhookTrigger], int]:
        items = list(
            db.scalars(
                select(WebhookTrigger)
                .order_by(WebhookTrigger.created_at.desc(), WebhookTrigger.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        total = db.scalar(select(func.count()).select_from(WebhookTrigger)) or 0
        return items, total

    def get_workflow_run(self, db: Session, trigger_id: int) -> WorkflowRun | None:
        return self.workflows.get_run_for_trigger(db, trigger_id)
