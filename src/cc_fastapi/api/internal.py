from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from cc_fastapi.api.dependencies import require_token
from cc_fastapi.db.session import get_db
from cc_fastapi.db.models import TaskStatus, WorkflowRunStatus
from cc_fastapi.schemas.internal import (
    ChangeRequestDetailResponse,
    ChangeRequestLatestTaskResponse,
    ChangeRequestListResponse,
    ChangeRequestResponse,
    ChangeRequestWorkflowResponse,
    MergeRequestTaskItemResponse,
    MergeRequestTaskListResponse,
)
from cc_fastapi.services.merge_requests import (
    ChangeRequestRecord,
    MergeRequestTaskRecord,
    MergeRequestTaskService,
)


router = APIRouter(prefix="/v1/internal", tags=["internal"])
merge_request_tasks = MergeRequestTaskService()


def _to_item(
    record: MergeRequestTaskRecord,
    *,
    include_result: bool = True,
) -> MergeRequestTaskItemResponse:
    task = record.task
    run = record.workflow_run
    trigger = record.webhook_trigger
    context = record.task_context
    payload = task.payload if isinstance(task.payload, dict) else {}
    run_context = run.context_json if isinstance(run.context_json, dict) else {}
    result = task.result if isinstance(task.result, dict) else None
    output_text = result.get("output_text") if result is not None else None
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
        result=result if include_result else None,
        output_text=str(output_text)
        if include_result and output_text is not None
        else None,
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


def _to_workflow(run) -> ChangeRequestWorkflowResponse:
    return ChangeRequestWorkflowResponse(
        id=run.id,
        workflow_name=run.workflow_name,
        workflow_version=run.workflow_version,
        status=run.status,
        event_type=run.event_type,
        skip_reason=run.skip_reason,
        error_message=run.error_message,
        created_at=run.created_at,
        updated_at=run.updated_at,
        finished_at=run.finished_at,
    )


def _to_change_request(record: ChangeRequestRecord) -> ChangeRequestResponse:
    parsed = record.parsed_payload
    change_request = parsed.change_request if parsed is not None else None
    task = record.latest_task
    return ChangeRequestResponse(
        provider=record.provider,
        resource_type=record.resource_type,
        project_path=record.project_path,
        pr_number=record.pr_number,
        title=change_request.title if change_request is not None else None,
        url=change_request.url if change_request is not None else None,
        state=(change_request.state if change_request is not None else None)
        or "unknown",
        status_source="webhook",
        action=change_request.action if change_request is not None else None,
        source_branch=(
            change_request.source_branch if change_request is not None else None
        ),
        target_branch=(
            change_request.target_branch if change_request is not None else None
        ),
        head_sha=change_request.head_sha if change_request is not None else None,
        merged_sha=change_request.merged_sha if change_request is not None else None,
        last_activity_at=record.workflow_run.created_at,
        latest_workflow=_to_workflow(record.workflow_run),
        latest_task=(
            ChangeRequestLatestTaskResponse(
                id=task.id,
                status=task.status,
                session_id=task.session_id,
                created_at=task.created_at,
                finished_at=task.finished_at,
            )
            if task is not None
            else None
        ),
    )


@router.get(
    "/change-requests",
    response_model=ChangeRequestListResponse,
    dependencies=[Depends(require_token)],
)
def list_change_requests(
    provider: str | None = Query(default=None, max_length=32),
    project_path: str | None = Query(default=None, max_length=255),
    pr_number: str | None = Query(default=None, max_length=128),
    states: list[str] | None = Query(default=None, alias="state"),
    search: str | None = Query(default=None, alias="q", max_length=200),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> ChangeRequestListResponse:
    try:
        records, total = merge_request_tasks.list_change_requests(
            db,
            provider=provider,
            project_path=project_path,
            pr_number=pr_number,
            states=states,
            search=search,
            offset=offset,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    return ChangeRequestListResponse(
        items=[_to_change_request(record) for record in records],
        total=total,
    )


@router.get(
    "/change-requests/detail",
    response_model=ChangeRequestDetailResponse,
    dependencies=[Depends(require_token)],
)
def get_change_request_detail(
    provider: str = Query(max_length=32),
    project_path: str = Query(max_length=255),
    pr_number: str = Query(max_length=128),
    task_id: str | None = Query(default=None, max_length=36),
    task_statuses: list[TaskStatus] | None = Query(default=None, alias="task_status"),
    workflow_statuses: list[WorkflowRunStatus] | None = Query(
        default=None, alias="workflow_status"
    ),
    role: str | None = Query(default=None, max_length=64),
    is_active: bool | None = None,
    has_result: bool | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    include_result: bool = True,
    task_offset: int = Query(default=0, ge=0),
    task_limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
) -> ChangeRequestDetailResponse:
    try:
        detail = merge_request_tasks.get_change_request_detail(
            db,
            provider=provider,
            project_path=project_path,
            pr_number=pr_number,
        )
        if detail is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="change request not found",
            )
        tasks, task_total = merge_request_tasks.list_tasks(
            db,
            provider=provider,
            project_path=project_path,
            pr_number=pr_number,
            task_id=task_id,
            task_statuses=task_statuses,
            workflow_statuses=workflow_statuses,
            role=role,
            is_active=is_active,
            has_result=has_result,
            created_from=created_from,
            created_to=created_to,
            offset=task_offset,
            limit=task_limit,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    return ChangeRequestDetailResponse(
        change_request=_to_change_request(detail.change_request),
        workflow_runs=[_to_workflow(run) for run in detail.workflow_runs],
        tasks=[_to_item(record, include_result=include_result) for record in tasks],
        task_total=task_total,
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
    return MergeRequestTaskListResponse(
        items=[_to_item(record) for record in records], total=total
    )
