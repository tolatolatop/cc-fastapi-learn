import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from cc_fastapi.core.config import get_settings
from cc_fastapi.db.models import TaskStatus
from cc_fastapi.db.session import get_db
from cc_fastapi.schemas.tasks import (
    TaskCancelResponse,
    TaskContextResponse,
    TaskCreateRequest,
    TaskCreateResponse,
    TaskItemResponse,
    TaskListResponse,
    TaskLogItemResponse,
    TaskLogListResponse,
)
from cc_fastapi.services.claude_client import validate_claude_agent_options
from cc_fastapi.services.queue import QueueNotFoundError, TaskQueueService


router = APIRouter(prefix="/v1/agent-tasks", tags=["agent-tasks"])
queue = TaskQueueService()
logger = logging.getLogger(__name__)


def require_token(x_api_token: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not settings.api_token:
        return
    if x_api_token != settings.api_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api token")


def _to_task_item(task) -> TaskItemResponse:
    return TaskItemResponse(
        id=task.id,
        status=task.status,
        queue_name=getattr(task, "queue_name", "default") or "default",
        priority=task.priority,
        attempt=task.attempt,
        max_attempts=task.max_attempts,
        agent_mode=task.agent_mode,
        unattended=task.unattended,
        created_at=task.created_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
        abandoned_at=task.abandoned_at,
        abandoned_reason=task.abandoned_reason,
        error_message=task.error_message,
        result=task.result,
    )


@router.post("", response_model=TaskCreateResponse, dependencies=[Depends(require_token)])
def create_task(payload: TaskCreateRequest, db: Session = Depends(get_db)) -> TaskCreateResponse:
    logger.debug(
        "create_task request received",
        extra={
            "event_type": "api_create_task_request",
            "queue_name": payload.queue_name or "default",
        },
    )
    try:
        validated_options = validate_claude_agent_options(payload.claude_agent_options)
    except ValueError as exc:
        logger.warning(
            "create_task request validation failed",
            extra={"event_type": "api_create_task_validation_failed", "reason": str(exc)},
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    try:
        task = queue.create_task(
            db,
            prompt=payload.prompt,
            model=payload.model,
            queue_name=payload.queue_name,
            metadata=payload.metadata,
            claude_agent_options=validated_options,
            priority=payload.priority,
            agent_mode=payload.agent_mode,
            unattended=payload.unattended,
            max_attempts=payload.max_attempts,
        )
    except QueueNotFoundError as exc:
        logger.warning(
            "create_task queue resolve failed",
            extra={"event_type": "api_create_task_queue_not_found", "queue_name": exc.queue_name},
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    logger.info(
        "task created via api",
        extra={"event_type": "api_create_task_success", "task_id": task.id, "queue_name": task.queue_name},
    )
    return TaskCreateResponse(task_id=task.id, status=task.status, queue_name=task.queue_name)


@router.get("/{task_id}", response_model=TaskItemResponse, dependencies=[Depends(require_token)])
def get_task(task_id: str, db: Session = Depends(get_db)) -> TaskItemResponse:
    task = queue.get_task(db, task_id)
    if not task:
        logger.warning("get_task not found", extra={"event_type": "api_get_task_not_found", "task_id": task_id})
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    return _to_task_item(task)


@router.get("/{task_id}/context", response_model=TaskContextResponse, dependencies=[Depends(require_token)])
def get_task_context(task_id: str, db: Session = Depends(get_db)) -> TaskContextResponse:
    task = queue.get_task(db, task_id)
    if not task:
        logger.warning(
            "get_task_context not found",
            extra={"event_type": "api_get_task_context_not_found", "task_id": task_id},
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")

    context = queue.get_task_context(db, task_id)
    messages = context.messages_json if context else []
    updated_at = context.updated_at if context else None
    return TaskContextResponse(task_id=task_id, messages=messages, updated_at=updated_at)


@router.get("", response_model=TaskListResponse, dependencies=[Depends(require_token)])
def list_tasks(
    status_filter: TaskStatus | None = Query(default=None, alias="status"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> TaskListResponse:
    logger.debug(
        "list_tasks request",
        extra={"event_type": "api_list_tasks", "reason": f"offset={offset},limit={limit},status={status_filter}"},
    )
    items, total = queue.list_tasks(db, status_filter, offset, limit)
    return TaskListResponse(items=[_to_task_item(item) for item in items], total=total)


@router.post("/{task_id}/cancel", response_model=TaskCancelResponse, dependencies=[Depends(require_token)])
def cancel_task(task_id: str, db: Session = Depends(get_db)) -> TaskCancelResponse:
    task = queue.cancel_task(db, task_id)
    if not task:
        logger.warning("cancel_task not found", extra={"event_type": "api_cancel_task_not_found", "task_id": task_id})
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    logger.info("task cancelled via api", extra={"event_type": "api_cancel_task", "task_id": task.id})
    return TaskCancelResponse(task_id=task.id, status=task.status)


@router.get("/{task_id}/logs", response_model=TaskLogListResponse, dependencies=[Depends(require_token)])
def list_task_logs(
    task_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> TaskLogListResponse:
    task = queue.get_task(db, task_id)
    if not task:
        logger.warning("list_task_logs not found", extra={"event_type": "api_task_logs_not_found", "task_id": task_id})
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    items, total = queue.list_logs(db, task_id, offset, limit)
    logger.debug(
        "list_task_logs request",
        extra={"event_type": "api_task_logs", "task_id": task_id, "reason": f"offset={offset},limit={limit},total={total}"},
    )
    return TaskLogListResponse(
        items=[
            TaskLogItemResponse(
                id=item.id,
                task_id=item.task_id,
                ts=item.ts,
                level=item.level,
                event_type=item.event_type,
                message=item.message,
                metadata=item.metadata_json,
            )
            for item in items
        ],
        total=total,
    )

