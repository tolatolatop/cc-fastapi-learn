from datetime import datetime
from typing import Any

from pydantic import BaseModel

from cc_fastapi.db.models import TaskStatus


class GitLabWebhookResponse(BaseModel):
    webhook_id: int
    task_id: str
    status: TaskStatus
    queue_name: str
    deduplicated: bool


class WebhookTriggerItemResponse(BaseModel):
    id: int
    provider: str
    event_type: str
    event_uuid: str | None
    webhook_uuid: str | None
    instance_url: str | None
    task_id: str
    task_status: TaskStatus
    payload: dict[str, Any]
    created_at: datetime


class WebhookTriggerListSummaryResponse(BaseModel):
    total: int
    event_types: list[str]


class WebhookTriggerListResponse(BaseModel):
    items: list[WebhookTriggerItemResponse]
    total: int
    summary: WebhookTriggerListSummaryResponse
