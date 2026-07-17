from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from cc_fastapi.api.dependencies import require_token
from cc_fastapi.db.session import get_db
from cc_fastapi.schemas.review_dashboard import (
    ReviewDashboardPullRequestDetailResponse,
    ReviewDashboardResponse,
)
from cc_fastapi.schemas.review_issues import ReviewIssueBatchResponse
from cc_fastapi.services.review_dashboard import ReviewDashboardService


router = APIRouter(
    prefix="/v1/review-dashboard",
    tags=["review-dashboard"],
    dependencies=[Depends(require_token)],
)
dashboard_service = ReviewDashboardService()


@router.get("", response_model=ReviewDashboardResponse)
def get_review_dashboard(
    provider: str | None = Query(default=None, max_length=32),
    project_path: str | None = Query(default=None, max_length=255),
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    outcome: Literal["all", "accepted", "unhandled", "pending"] = "all",
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> ReviewDashboardResponse:
    if created_from is not None and created_to is not None and created_from > created_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="created_from must not be later than created_to",
        )
    return ReviewDashboardResponse(
        **dashboard_service.dashboard(
            db,
            provider=provider,
            project_path=project_path,
            created_from=created_from,
            created_to=created_to,
            outcome=outcome,
            offset=offset,
            limit=limit,
        )
    )


@router.get("/pull-request", response_model=ReviewDashboardPullRequestDetailResponse)
def get_review_dashboard_pull_request(
    provider: str = Query(max_length=32),
    project_path: str = Query(max_length=255),
    pr_number: str = Query(max_length=128),
    db: Session = Depends(get_db),
) -> ReviewDashboardPullRequestDetailResponse:
    detail = dashboard_service.pull_request_detail(
        db,
        provider=provider,
        project_path=project_path,
        pr_number=pr_number,
    )
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="review pull request not found",
        )
    detail["batches"] = [
        ReviewIssueBatchResponse.model_validate(batch) for batch in detail["batches"]
    ]
    return ReviewDashboardPullRequestDetailResponse(**detail)
