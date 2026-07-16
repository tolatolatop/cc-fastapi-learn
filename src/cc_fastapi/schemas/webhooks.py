from datetime import datetime
from typing import Any

from pydantic import BaseModel

from cc_fastapi.db.models import TaskStatus, WorkflowRunStatus


class GitLabWebhookResponse(BaseModel):
    webhook_id: int
    task_id: str | None
    status: TaskStatus | None
    queue_name: str | None
    deduplicated: bool
    workflow_run_id: str
    workflow_status: WorkflowRunStatus
    skip_reason: str | None


class WebhookTriggerItemResponse(BaseModel):
    id: int
    provider: str
    event_type: str
    event_uuid: str | None
    webhook_uuid: str | None
    instance_url: str | None
    task_id: str | None
    payload: dict[str, Any]
    created_at: datetime
    workflow_run_id: str | None
    workflow_status: WorkflowRunStatus | None
    skip_reason: str | None


class WebhookTriggerListResponse(BaseModel):
    items: list[WebhookTriggerItemResponse]
    total: int
