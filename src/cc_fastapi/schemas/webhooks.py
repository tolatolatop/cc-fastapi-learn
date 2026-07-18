from datetime import datetime
from typing import Any

from pydantic import BaseModel

from cc_fastapi.db.models import TaskStatus, WorkflowRunStatus


class WebhookResponse(BaseModel):
    webhook_id: int
    task_id: str | None
    status: TaskStatus | None
    queue_name: str | None
    deduplicated: bool
    workflow_run_id: str
    workflow_status: WorkflowRunStatus
    skip_reason: str | None


class GitLabWebhookResponse(WebhookResponse):
    pass


class GitHubWebhookResponse(WebhookResponse):
    pass


class WebhookTriggerItemResponse(BaseModel):
    id: int
    provider: str
    event_type: str
    event_uuid: str | None
    webhook_uuid: str | None
    instance_url: str | None
    task_id: str | None
    task_status: TaskStatus | None
    payload: dict[str, Any]
    created_at: datetime
    workflow_run_id: str | None
    workflow_status: WorkflowRunStatus | None
    skip_reason: str | None


class WebhookTriggerListSummaryResponse(BaseModel):
    total: int
    event_types: list[str]
    providers: list[str]


class WebhookTriggerListResponse(BaseModel):
    items: list[WebhookTriggerItemResponse]
    total: int
    summary: WebhookTriggerListSummaryResponse
