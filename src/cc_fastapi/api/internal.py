from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from cc_fastapi.api.dependencies import require_token
from cc_fastapi.db.session import get_db
from cc_fastapi.schemas.internal import MergeRequestTaskItemResponse, MergeRequestTaskListResponse
from cc_fastapi.services.merge_requests import MergeRequestTaskRecord, MergeRequestTaskService


router = APIRouter(prefix="/v1/internal", tags=["internal"])
merge_request_tasks = MergeRequestTaskService()


def _to_item(record: MergeRequestTaskRecord) -> MergeRequestTaskItemResponse:
    task = record.task
    run = record.workflow_run
    trigger = record.webhook_trigger
    context = record.task_context
    payload = task.payload if isinstance(task.payload, dict) else {}
    run_context = run.context_json if isinstance(run.context_json, dict) else {}
    superseded_by = run_context.get("superseded_by_workflow_run_id")
    return MergeRequestTaskItemResponse(
        id=task.id,
        status=task.status,
        queue_name=task.queue_name,
        priority=task.priority,
        payload=payload,
        prompt=str(payload.get("prompt", "")),
        model=str(payload.get("model", "")),
        claude_agent_options=(
            payload.get("claude_agent_options", {})
            if isinstance(payload.get("claude_agent_options", {}), dict)
            else {}
        ),
        metadata=task.metadata_json,
        result=task.result,
        agent_mode=task.agent_mode,
        unattended=task.unattended,
        attempt=task.attempt,
        max_attempts=task.max_attempts,
        worker_id=task.worker_id,
        session_id=task.session_id,
        error_message=task.error_message,
        created_at=task.created_at,
        scheduled_at=task.scheduled_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
        abandoned_at=task.abandoned_at,
        abandoned_reason=task.abandoned_reason,
        queue_expire_at=task.queue_expire_at,
        running_expire_at=task.running_expire_at,
        context_messages=context.messages_json if context is not None else [],
        context_updated_at=context.updated_at if context is not None else None,
        workflow_run_id=run.id,
        workflow_name=run.workflow_name,
        workflow_version=run.workflow_version,
        workflow_status=run.status,
        superseded_by_workflow_run_id=str(superseded_by) if superseded_by else None,
        role=record.link.role,
        ordinal=record.link.ordinal,
        is_active=record.link.is_active,
        webhook_id=trigger.id if trigger is not None else None,
        event_type=run.event_type,
        event_uuid=run.event_uuid,
        webhook_uuid=run.webhook_uuid,
        instance_url=run.instance_url,
    )


@router.get(
    "/gitlab/merge-request-tasks",
    response_model=MergeRequestTaskListResponse,
    dependencies=[Depends(require_token)],
)
def list_gitlab_merge_request_tasks(
    project_path: str = Query(min_length=1, max_length=255),
    merge_request_iid: int = Query(ge=1),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
) -> MergeRequestTaskListResponse:
    records, total = merge_request_tasks.list_gitlab_tasks(
        db,
        project_path=project_path.strip(),
        merge_request_iid=merge_request_iid,
        offset=offset,
        limit=limit,
    )
    return MergeRequestTaskListResponse(items=[_to_item(record) for record in records], total=total)
