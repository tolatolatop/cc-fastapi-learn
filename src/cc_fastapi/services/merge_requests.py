from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cc_fastapi.db.models import (
    AgentTask,
    AgentTaskContext,
    WebhookTrigger,
    WorkflowCorrelation,
    WorkflowRun,
    WorkflowTaskLink,
)


@dataclass(frozen=True)
class MergeRequestTaskRecord:
    task: AgentTask
    link: WorkflowTaskLink
    workflow_run: WorkflowRun
    webhook_trigger: WebhookTrigger | None
    task_context: AgentTaskContext | None


class MergeRequestTaskService:
    def list_gitlab_tasks(
        self,
        db: Session,
        *,
        project_path: str,
        merge_request_iid: int,
        offset: int,
        limit: int,
    ) -> tuple[list[MergeRequestTaskRecord], int]:
        filters = (
            WorkflowCorrelation.provider == "gitlab",
            WorkflowCorrelation.resource_type == "merge_request",
            WorkflowCorrelation.project_path == project_path,
            WorkflowCorrelation.resource_id == str(merge_request_iid),
        )
        rows = db.execute(
            select(AgentTask, WorkflowTaskLink, WorkflowRun, WebhookTrigger, AgentTaskContext)
            .join(WorkflowTaskLink, WorkflowTaskLink.task_id == AgentTask.id)
            .join(WorkflowRun, WorkflowRun.id == WorkflowTaskLink.workflow_run_id)
            .join(WorkflowCorrelation, WorkflowCorrelation.workflow_run_id == WorkflowRun.id)
            .outerjoin(WebhookTrigger, WebhookTrigger.id == WorkflowRun.webhook_trigger_id)
            .outerjoin(AgentTaskContext, AgentTaskContext.task_id == AgentTask.id)
            .where(*filters)
            .order_by(AgentTask.created_at.desc(), WorkflowTaskLink.ordinal.asc())
            .offset(offset)
            .limit(limit)
        ).all()
        total = db.scalar(
            select(func.count())
            .select_from(WorkflowTaskLink)
            .join(WorkflowRun, WorkflowRun.id == WorkflowTaskLink.workflow_run_id)
            .join(WorkflowCorrelation, WorkflowCorrelation.workflow_run_id == WorkflowRun.id)
            .where(*filters)
        ) or 0
        return (
            [
                MergeRequestTaskRecord(
                    task=row[0],
                    link=row[1],
                    workflow_run=row[2],
                    webhook_trigger=row[3],
                    task_context=row[4],
                )
                for row in rows
            ],
            total,
        )
