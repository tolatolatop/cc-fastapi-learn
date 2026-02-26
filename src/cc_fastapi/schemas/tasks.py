from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from cc_fastapi.db.models import TaskStatus


class TaskCreateRequest(BaseModel):
    prompt: str = Field(min_length=1)
    model: str | None = None
    queue_name: str | None = None
    metadata: dict[str, Any] | None = None
    claude_agent_options: dict[str, Any] | None = None
    priority: int = 0
    agent_mode: bool = True
    unattended: bool = True
    max_attempts: int | None = None


class TaskCreateResponse(BaseModel):
    task_id: str
    status: TaskStatus
    queue_name: str


class TaskItemResponse(BaseModel):
    id: str
    status: TaskStatus
    queue_name: str
    priority: int
    attempt: int
    max_attempts: int
    agent_mode: bool
    unattended: bool
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    abandoned_at: datetime | None
    abandoned_reason: str | None
    error_message: str | None
    result: dict[str, Any] | None


class TaskListResponse(BaseModel):
    items: list[TaskItemResponse]
    total: int


class TaskCancelResponse(BaseModel):
    task_id: str
    status: TaskStatus


class TaskLogItemResponse(BaseModel):
    id: int
    task_id: str
    ts: datetime
    level: str
    event_type: str
    message: str
    metadata: dict[str, Any] | None


class TaskLogListResponse(BaseModel):
    items: list[TaskLogItemResponse]
    total: int


class TaskContextResponse(BaseModel):
    task_id: str
    messages: list[str]
    updated_at: datetime | None

