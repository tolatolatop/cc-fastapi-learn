from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from cc_fastapi.api.dependencies import require_token
from cc_fastapi.db.models import (
    ReviewBatchStatus,
    ReviewIssueSeverity,
    ReviewIssueVerificationStatus,
)
from cc_fastapi.db.session import get_db
from cc_fastapi.schemas.review_issues import (
    ReviewIssueBatchCreateRequest,
    ReviewIssueBatchListResponse,
    ReviewIssueBatchResponse,
    ReviewIssueBatchUpdateRequest,
    ReviewIssueBulkCreateRequest,
    ReviewIssueBulkVerificationRequest,
    ReviewIssueListResponse,
    ReviewIssueResponse,
    ReviewIssueStatisticsResponse,
    ReviewIssueVerificationUpdateRequest,
)
from cc_fastapi.services.review_issues import (
    ReviewIssueConflictError,
    ReviewIssueNotFoundError,
    ReviewIssueReferenceError,
    ReviewIssueService,
)


batch_router = APIRouter(
    prefix="/v1/review-issue-batches",
    tags=["review-issues"],
    dependencies=[Depends(require_token)],
)
issue_router = APIRouter(
    prefix="/v1/review-issues",
    tags=["review-issues"],
    dependencies=[Depends(require_token)],
)
reviews = ReviewIssueService()


def _raise_service_error(exc: Exception) -> None:
    if isinstance(exc, ReviewIssueNotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, ReviewIssueReferenceError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, ReviewIssueConflictError):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    raise exc


@batch_router.post("", response_model=ReviewIssueBatchResponse, status_code=status.HTTP_201_CREATED)
def create_review_issue_batch(
    payload: ReviewIssueBatchCreateRequest,
    db: Session = Depends(get_db),
) -> ReviewIssueBatchResponse:
    try:
        batch = reviews.create_batch(db, payload.model_dump())
    except (ReviewIssueNotFoundError, ReviewIssueReferenceError, ReviewIssueConflictError) as exc:
        _raise_service_error(exc)
        raise AssertionError("unreachable")
    return ReviewIssueBatchResponse.model_validate(batch)


@batch_router.get("", response_model=ReviewIssueBatchListResponse)
def list_review_issue_batches(
    provider: str | None = Query(default=None, max_length=32),
    project_path: str | None = Query(default=None, max_length=255),
    pr_number: str | None = Query(default=None, max_length=128),
    statuses: list[ReviewBatchStatus] | None = Query(default=None, alias="status"),
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> ReviewIssueBatchListResponse:
    items, total = reviews.list_batches(
        db,
        provider=provider,
        project_path=project_path,
        pr_number=pr_number,
        statuses=statuses,
        created_from=created_from,
        created_to=created_to,
        offset=offset,
        limit=limit,
    )
    return ReviewIssueBatchListResponse(
        items=[ReviewIssueBatchResponse.model_validate(item) for item in items],
        total=total,
    )


@batch_router.get("/{batch_id}", response_model=ReviewIssueBatchResponse)
def get_review_issue_batch(
    batch_id: str,
    db: Session = Depends(get_db),
) -> ReviewIssueBatchResponse:
    batch = reviews.get_batch(db, batch_id)
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="review issue batch not found")
    return ReviewIssueBatchResponse.model_validate(batch)


@batch_router.patch("/{batch_id}", response_model=ReviewIssueBatchResponse)
def update_review_issue_batch(
    batch_id: str,
    payload: ReviewIssueBatchUpdateRequest,
    db: Session = Depends(get_db),
) -> ReviewIssueBatchResponse:
    try:
        batch = reviews.update_batch(db, batch_id, payload.model_dump(exclude_unset=True))
    except (ReviewIssueNotFoundError, ReviewIssueReferenceError, ReviewIssueConflictError) as exc:
        _raise_service_error(exc)
        raise AssertionError("unreachable")
    return ReviewIssueBatchResponse.model_validate(batch)


@batch_router.post(
    "/{batch_id}/issues",
    response_model=ReviewIssueListResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_review_issues(
    batch_id: str,
    payload: ReviewIssueBulkCreateRequest,
    db: Session = Depends(get_db),
) -> ReviewIssueListResponse:
    try:
        items = reviews.create_issues(
            db,
            batch_id,
            [item.model_dump() for item in payload.items],
        )
    except (ReviewIssueNotFoundError, ReviewIssueReferenceError, ReviewIssueConflictError) as exc:
        _raise_service_error(exc)
        raise AssertionError("unreachable")
    return ReviewIssueListResponse(
        items=[ReviewIssueResponse.model_validate(item) for item in items],
        total=len(items),
    )


@batch_router.patch("/{batch_id}/issues", response_model=ReviewIssueListResponse)
def verify_review_issues(
    batch_id: str,
    payload: ReviewIssueBulkVerificationRequest,
    db: Session = Depends(get_db),
) -> ReviewIssueListResponse:
    try:
        items = reviews.verify_issues(
            db,
            batch_id,
            [
                {
                    "id": item.id,
                    "status": item.status,
                    "note": item.note,
                }
                for item in payload.items
            ],
        )
    except (ReviewIssueNotFoundError, ReviewIssueReferenceError, ReviewIssueConflictError) as exc:
        _raise_service_error(exc)
        raise AssertionError("unreachable")
    return ReviewIssueListResponse(
        items=[ReviewIssueResponse.model_validate(item) for item in items],
        total=len(items),
    )


@issue_router.get("/summary", response_model=ReviewIssueStatisticsResponse)
def summarize_review_issues(
    provider: str | None = Query(default=None, max_length=32),
    project_path: str | None = Query(default=None, max_length=255),
    pr_number: str | None = Query(default=None, max_length=128),
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    db: Session = Depends(get_db),
) -> ReviewIssueStatisticsResponse:
    return ReviewIssueStatisticsResponse(
        **reviews.summarize(
            db,
            provider=provider,
            project_path=project_path,
            pr_number=pr_number,
            created_from=created_from,
            created_to=created_to,
        )
    )


@issue_router.get("", response_model=ReviewIssueListResponse)
def list_review_issues(
    batch_id: str | None = Query(default=None, max_length=36),
    provider: str | None = Query(default=None, max_length=32),
    project_path: str | None = Query(default=None, max_length=255),
    pr_number: str | None = Query(default=None, max_length=128),
    severities: list[ReviewIssueSeverity] | None = Query(default=None, alias="severity"),
    verification_statuses: list[ReviewIssueVerificationStatus] | None = Query(
        default=None, alias="status"
    ),
    category: str | None = Query(default=None, max_length=64),
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    batch_created_from: datetime | None = None,
    batch_created_to: datetime | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> ReviewIssueListResponse:
    items, total = reviews.list_issues(
        db,
        batch_id=batch_id,
        provider=provider,
        project_path=project_path,
        pr_number=pr_number,
        severities=severities,
        verification_statuses=verification_statuses,
        category=category,
        created_from=created_from,
        created_to=created_to,
        batch_created_from=batch_created_from,
        batch_created_to=batch_created_to,
        offset=offset,
        limit=limit,
    )
    return ReviewIssueListResponse(
        items=[ReviewIssueResponse.model_validate(item) for item in items],
        total=total,
    )


@issue_router.get("/{issue_id}", response_model=ReviewIssueResponse)
def get_review_issue(
    issue_id: str,
    db: Session = Depends(get_db),
) -> ReviewIssueResponse:
    issue = reviews.get_issue(db, issue_id)
    if issue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="review issue not found")
    return ReviewIssueResponse.model_validate(issue)


@issue_router.patch("/{issue_id}", response_model=ReviewIssueResponse)
def update_review_issue_verification(
    issue_id: str,
    payload: ReviewIssueVerificationUpdateRequest,
    db: Session = Depends(get_db),
) -> ReviewIssueResponse:
    issue = reviews.get_issue(db, issue_id)
    if issue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="review issue not found")
    try:
        updated = reviews.verify_issues(
            db,
            issue.batch_id,
            [{"id": issue.id, "status": payload.status, "note": payload.note}],
        )[0]
    except (ReviewIssueNotFoundError, ReviewIssueReferenceError, ReviewIssueConflictError) as exc:
        _raise_service_error(exc)
        raise AssertionError("unreachable")
    return ReviewIssueResponse.model_validate(updated)
