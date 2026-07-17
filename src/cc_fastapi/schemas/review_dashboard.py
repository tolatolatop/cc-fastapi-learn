from datetime import date, datetime

from pydantic import BaseModel

from cc_fastapi.db.models import (
    ReviewBatchStatus,
    TaskStatus,
)
from cc_fastapi.schemas.review_issues import ReviewIssueBatchResponse


class ReviewDashboardSummaryResponse(BaseModel):
    pull_request_total: int
    batch_total: int
    issue_total: int
    accepted_issues: int
    merged_unhandled_issues: int
    pending_issues: int
    acceptance_rate: float | None


class ReviewDashboardTrendPointResponse(BaseModel):
    date: date
    issue_total: int
    accepted_issues: int
    merged_unhandled_issues: int
    pending_issues: int


class ReviewDashboardRepositoryResponse(BaseModel):
    provider: str
    project_path: str
    pull_request_total: int
    issue_total: int


class ReviewDashboardPullRequestResponse(BaseModel):
    provider: str
    project_path: str
    pr_number: str
    pr_url: str | None
    latest_batch_id: str
    latest_batch_status: ReviewBatchStatus
    batch_total: int
    issue_total: int
    accepted_issues: int
    merged_unhandled_issues: int
    pending_issues: int
    latest_activity_at: datetime
    task_total: int
    task_status_counts: dict[TaskStatus, int]


class ReviewDashboardTaskResponse(BaseModel):
    id: str
    batch_id: str
    role: str
    status: TaskStatus
    session_id: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None


class ReviewDashboardResponse(BaseModel):
    summary: ReviewDashboardSummaryResponse
    timeline: list[ReviewDashboardTrendPointResponse]
    repositories: list[ReviewDashboardRepositoryResponse]
    items: list[ReviewDashboardPullRequestResponse]
    total: int


class ReviewDashboardPullRequestDetailResponse(BaseModel):
    pull_request: ReviewDashboardPullRequestResponse
    batches: list[ReviewIssueBatchResponse]
    tasks: list[ReviewDashboardTaskResponse]
