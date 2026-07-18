from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

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


class WebhookRepositoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    project_path: str
    web_url: str | None


class WebhookActorResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    display_name: str
    username: str | None


class WebhookChangeRequestResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    resource_type: str
    number: str
    action: str | None
    source_branch: str | None
    target_branch: str | None
    head_sha: str | None


class WebhookPayloadResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    provider: str
    event_type: str
    event_kind: str
    repository: WebhookRepositoryResponse | None
    actor: WebhookActorResponse | None
    ref: str | None
    change_request: WebhookChangeRequestResponse | None


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
    parsed_payload: WebhookPayloadResponse | None
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
