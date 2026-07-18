from typing import Any

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cc_fastapi.db.models import (
    AgentTask,
    TaskStatus,
    WebhookDeduplicationKey,
    WebhookTrigger,
    WorkflowCorrelation,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowStepRun,
    WorkflowStepStatus,
    WorkflowTaskLink,
    utc_now,
)
from cc_fastapi.workflows import WorkflowEngine, build_default_workflow_engine
from cc_fastapi.workflows.base import WorkflowEvent, WorkflowTemplateError
from cc_fastapi.workflows.github_prompt import github_pull_request_correlation
from cc_fastapi.workflows.gitlab_prompt import gitlab_merge_request_correlation


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
            workflow_name=f"{trigger.provider}_prompt_task",
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
        correlation = None
        if trigger.provider == "gitlab":
            correlation = gitlab_merge_request_correlation(trigger.payload_json)
        elif trigger.provider == "github":
            correlation = github_pull_request_correlation(trigger.payload_json)
        if correlation is not None:
            db.add(
                WorkflowCorrelation(
                    workflow_run_id=run.id,
                    provider=correlation.provider,
                    resource_type=correlation.resource_type,
                    project_path=correlation.project_path,
                    resource_id=correlation.resource_id,
                )
            )
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

    def trigger_task(
        self,
        db: Session,
        *,
        provider: str,
        payload: dict[str, Any],
        event_type: str,
        event_uuid: str | None,
        webhook_uuid: str | None,
        instance_url: str | None,
        prompt_template_path: str,
        queue_name: str | None,
        provider_metadata: dict[str, Any] | None = None,
    ) -> tuple[WebhookTrigger, AgentTask | None, bool, WorkflowRun]:
        provider = provider.strip().lower()
        if not provider:
            raise ValueError("webhook provider must not be empty")
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
                    "provider_metadata": provider_metadata or {},
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
        return self.trigger_task(
            db,
            provider="gitlab",
            payload=payload,
            event_type=event_type,
            event_uuid=event_uuid,
            webhook_uuid=webhook_uuid,
            instance_url=instance_url,
            prompt_template_path=prompt_template_path,
            queue_name=queue_name,
        )

    def trigger_github_task(
        self,
        db: Session,
        *,
        payload: dict[str, Any],
        event_type: str,
        delivery_id: str | None,
        hook_id: str | None,
        instance_url: str,
        prompt_template_path: str,
        queue_name: str | None,
    ) -> tuple[WebhookTrigger, AgentTask | None, bool, WorkflowRun]:
        return self.trigger_task(
            db,
            provider="github",
            payload=payload,
            event_type=event_type,
            event_uuid=delivery_id,
            webhook_uuid=delivery_id,
            instance_url=instance_url,
            prompt_template_path=prompt_template_path,
            queue_name=queue_name,
            provider_metadata={
                "delivery_id": delivery_id,
                "hook_id": hook_id,
            },
        )

    def list_triggers(
        self,
        db: Session,
        offset: int,
        limit: int,
        event_type: str | None = None,
        search: str | None = None,
        provider: str | None = None,
    ) -> tuple[list[tuple[WebhookTrigger, TaskStatus | None]], int]:
        query = select(WebhookTrigger, AgentTask.status).outerjoin(
            AgentTask, AgentTask.id == WebhookTrigger.task_id
        )
        count_query = select(func.count()).select_from(WebhookTrigger)
        filters = []
        if provider:
            filters.append(WebhookTrigger.provider == provider.strip().lower())
        if event_type:
            filters.append(WebhookTrigger.event_type == event_type)
        if search and (normalized_search := search.strip()):
            pattern = f"%{normalized_search}%"
            filters.append(
                or_(
                    WebhookTrigger.provider.ilike(pattern),
                    WebhookTrigger.event_type.ilike(pattern),
                    WebhookTrigger.event_uuid.ilike(pattern),
                    WebhookTrigger.webhook_uuid.ilike(pattern),
                    WebhookTrigger.instance_url.ilike(pattern),
                    WebhookTrigger.task_id.ilike(pattern),
                    cast(WebhookTrigger.payload_json, String).ilike(pattern),
                )
            )
        if filters:
            query = query.where(*filters)
            count_query = count_query.where(*filters)
        items = list(
            db.execute(
                query.order_by(WebhookTrigger.created_at.desc(), WebhookTrigger.id.desc())
                .offset(offset)
                .limit(limit)
            ).tuples()
        )
        total = db.scalar(count_query) or 0
        return items, total

    def get_workflow_run(self, db: Session, trigger_id: int) -> WorkflowRun | None:
        return self.workflows.get_run_for_trigger(db, trigger_id)

    def summarize_triggers(self, db: Session) -> tuple[int, list[str], list[str]]:
        total = db.scalar(select(func.count()).select_from(WebhookTrigger)) or 0
        event_types = list(
            db.scalars(select(WebhookTrigger.event_type).distinct().order_by(WebhookTrigger.event_type.asc()))
        )
        providers = list(
            db.scalars(select(WebhookTrigger.provider).distinct().order_by(WebhookTrigger.provider.asc()))
        )
        return total, event_types, providers
