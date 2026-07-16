from datetime import datetime
from typing import Any

from pydantic import BaseModel

from cc_fastapi.db.models import TaskStatus, WorkflowRunStatus


class MergeRequestTaskItemResponse(BaseModel):
    id: str
    status: TaskStatus
    queue_name: str
    priority: int
    payload: dict[str, Any]
    prompt: str
    model: str
    claude_agent_options: dict[str, Any]
    metadata: dict[str, Any] | None
    result: dict[str, Any] | None
    agent_mode: bool
    unattended: bool
    attempt: int
    max_attempts: int
    worker_id: str | None
    session_id: str | None
    error_message: str | None
    created_at: datetime
    scheduled_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    abandoned_at: datetime | None
    abandoned_reason: str | None
    queue_expire_at: datetime
    running_expire_at: datetime | None
    context_messages: list[str]
    context_updated_at: datetime | None
    workflow_run_id: str
    workflow_name: str
    workflow_version: str
    workflow_status: WorkflowRunStatus
    superseded_by_workflow_run_id: str | None
    role: str
    ordinal: int
    is_active: bool
    webhook_id: int | None
    event_type: str
    event_uuid: str | None
    webhook_uuid: str | None
    instance_url: str | None


class MergeRequestTaskListResponse(BaseModel):
    items: list[MergeRequestTaskItemResponse]
    total: int
