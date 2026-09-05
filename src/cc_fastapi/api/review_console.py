from datetime import datetime
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from cc_fastapi.api.dependencies import require_review_console_token
from cc_fastapi.db.models import ReviewIssueDecisionStatus, ReviewIssueSeverity
from cc_fastapi.db.session import get_db
from cc_fastapi.schemas.review_console import (
    ReviewConsoleIssueListResponse,
    ReviewConsoleIssueResponse,
    ReviewConsolePullRequestListResponse,
    ReviewConsolePullRequestResponse,
    ReviewConsoleRepositoryListResponse,
    ReviewConsoleStatisticsResponse,
    ReviewConsoleStatusChangeListResponse,
    ReviewConsoleStatusChangeResponse,
    ReviewConsoleStatusUpdateRequest,
)
from cc_fastapi.services.review_console import (
    ReviewConsoleConflictError,
    ReviewConsoleNotFoundError,
    ReviewConsoleService,
)

router = APIRouter(
    prefix="/v1/review-console",
    tags=["review-console-integration"],
    dependencies=[Depends(require_review_console_token)],
)
service = ReviewConsoleService()


def _issue_response(issue, batch) -> ReviewConsoleIssueResponse:
    return ReviewConsoleIssueResponse(
        id=issue.id,
        batch_id=issue.batch_id,
        provider=batch.provider,
        project_path=batch.project_path,
        pr_number=batch.pr_number,
        pr_url=batch.pr_url,
        issue_no=issue.issue_no,
        severity=issue.severity,
        category=issue.category,
        title=issue.title,
        description=issue.description,
        file_path=issue.file_path,
        line_number=issue.line_number,
        status=issue.decision_status,
        reason_code=issue.decision_reason_code,
        note=issue.decision_note,
        decided_by_id=issue.decided_by_id,
        decided_by_name=issue.decided_by_name,
        decided_at=issue.decided_at,
        verification_status=issue.verification_status,
        review_head_sha=batch.review_head_sha,
        merged_sha=batch.merged_sha,
        batch_status=batch.status,
        batch_created_at=batch.created_at,
        batch_extracted_at=batch.extracted_at,
        batch_verified_at=batch.verified_at,
        created_at=issue.created_at,
        updated_at=issue.updated_at,
    )


@router.get("/repositories", response_model=ReviewConsoleRepositoryListResponse)
def list_repositories(
    db: Session = Depends(get_db),
) -> ReviewConsoleRepositoryListResponse:
    return ReviewConsoleRepositoryListResponse(items=service.list_repositories(db))


@router.get("/issues", response_model=ReviewConsoleIssueListResponse)
def list_issues(
    provider: str = Query(max_length=32),
    project_path: str = Query(max_length=255),
    pr_number: str | None = Query(default=None, max_length=128),
    statuses: list[ReviewIssueDecisionStatus] | None = Query(
        default=None, alias="status"
    ),
    severities: list[ReviewIssueSeverity] | None = Query(
        default=None, alias="severity"
    ),
    query: str | None = Query(default=None, alias="q", max_length=200),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> ReviewConsoleIssueListResponse:
    rows, total = service.list_issues(
        db,
        provider=provider,
        project_path=project_path,
        pr_number=pr_number,
        statuses=statuses,
        severities=severities,
        query=query,
        offset=offset,
        limit=limit,
    )
    return ReviewConsoleIssueListResponse(
        items=[_issue_response(issue, batch) for issue, batch in rows], total=total
    )


@router.get("/pull-requests", response_model=ReviewConsolePullRequestListResponse)
def list_pull_requests(
    provider: str = Query(max_length=32),
    project_path: str = Query(max_length=255),
    completion_statuses: list[str] | None = Query(default=None, alias="status"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> ReviewConsolePullRequestListResponse:
    allowed = {"processing", "pending", "completed", "no_issues", "failed"}
    if completion_statuses and not set(completion_statuses).issubset(allowed):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="invalid pull request completion status",
        )
    items, total = service.list_pull_requests(
        db,
        provider=provider,
        project_path=project_path,
        completion_statuses=completion_statuses,
        offset=offset,
        limit=limit,
    )
    return ReviewConsolePullRequestListResponse(items=items, total=total)


@router.get("/pull-request", response_model=ReviewConsolePullRequestResponse)
def get_pull_request(
    provider: str = Query(max_length=32),
    project_path: str = Query(max_length=255),
    pr_number: str = Query(max_length=128),
    db: Session = Depends(get_db),
) -> ReviewConsolePullRequestResponse:
    try:
        return ReviewConsolePullRequestResponse(
            **service.get_pull_request(
                db,
                provider=provider,
                project_path=project_path,
                pr_number=pr_number,
            )
        )
    except ReviewConsoleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


@router.get("/issues/{issue_id}", response_model=ReviewConsoleIssueResponse)
def get_issue(
    issue_id: str, db: Session = Depends(get_db)
) -> ReviewConsoleIssueResponse:
    try:
        issue, batch = service.get_issue(db, issue_id)
    except ReviewConsoleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    return _issue_response(issue, batch)


@router.put("/issues/{issue_id}/status", response_model=ReviewConsoleIssueResponse)
def update_status(
    issue_id: str,
    payload: ReviewConsoleStatusUpdateRequest,
    actor_id: str = Header(alias="X-Review-Actor-Id", min_length=1, max_length=128),
    actor_name: str = Header(
        alias="X-Review-Actor-Name", min_length=1, max_length=2048
    ),
    db: Session = Depends(get_db),
) -> ReviewConsoleIssueResponse:
    decoded_actor_name = unquote(actor_name).strip()
    if not decoded_actor_name or len(decoded_actor_name) > 255:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="review actor name must contain 1 to 255 characters",
        )
    try:
        issue, batch = service.update_status(
            db,
            issue_id=issue_id,
            new_status=payload.status,
            reason_code=payload.reason_code,
            note=payload.note,
            expected_updated_at=payload.expected_updated_at,
            actor_id=actor_id,
            actor_name=decoded_actor_name,
        )
    except ReviewConsoleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ReviewConsoleConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return _issue_response(issue, batch)


@router.get(
    "/issues/{issue_id}/history",
    response_model=ReviewConsoleStatusChangeListResponse,
)
def list_history(
    issue_id: str, db: Session = Depends(get_db)
) -> ReviewConsoleStatusChangeListResponse:
    try:
        items = service.list_history(db, issue_id)
    except ReviewConsoleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    return ReviewConsoleStatusChangeListResponse(
        items=[
            ReviewConsoleStatusChangeResponse(
                id=item.id,
                issue_id=item.issue_id,
                previous_status=item.previous_status,
                new_status=item.new_status,
                previous_note=item.previous_note,
                new_note=item.new_note,
                previous_reason_code=item.previous_reason_code,
                new_reason_code=item.new_reason_code,
                actor_id=item.actor_id,
                actor_name=item.actor_name,
                dimension=item.dimension,
                source=item.source,
                created_at=item.created_at,
            )
            for item in items
        ]
    )


@router.get("/statistics", response_model=ReviewConsoleStatisticsResponse)
def statistics(
    created_from: datetime | None = Query(default=None),
    created_to: datetime | None = Query(default=None),
    repository: list[str] | None = Query(default=None),
    db: Session = Depends(get_db),
) -> ReviewConsoleStatisticsResponse:
    if created_from is not None and created_to is not None and created_from > created_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="created_from must not be later than created_to",
        )
    scopes: list[tuple[str, str]] | None = None
    if repository is not None:
        scopes = []
        for value in repository:
            provider, separator, project_path = value.partition("/")
            if not separator or not provider.strip() or not project_path.strip():
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="repository must use provider/project_path format",
                )
            scopes.append((provider.strip().lower(), project_path.strip().strip("/")))
    return ReviewConsoleStatisticsResponse(
        **service.statistics(
            db,
            created_from=created_from,
            created_to=created_to,
            repositories=scopes,
        )
    )
