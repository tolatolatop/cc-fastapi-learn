from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from cc_fastapi.db.models import (
    ReviewBatchStatus,
    ReviewIssueDecisionReason,
    ReviewIssueDecisionStatus,
    ReviewIssueSeverity,
    ReviewIssueVerificationStatus,
)


class ReviewConsoleRepositoryResponse(BaseModel):
    provider: str
    project_path: str
    issue_total: int
    pending_total: int


class ReviewConsoleRepositoryListResponse(BaseModel):
    items: list[ReviewConsoleRepositoryResponse]


class ReviewConsolePullRequestResponse(BaseModel):
    provider: str
    project_path: str
    pr_number: str
    pr_url: str | None
    completion_status: Literal[
        "processing", "pending", "completed", "no_issues", "failed"
    ]
    batch_total: int
    issue_total: int
    reviewed_total: int
    pending_total: int
    updated_at: datetime


class ReviewConsolePullRequestListResponse(BaseModel):
    items: list[ReviewConsolePullRequestResponse]
    total: int


class ReviewConsoleIssueResponse(BaseModel):
    id: str
    batch_id: str
    provider: str
    project_path: str
    pr_number: str
    pr_url: str | None
    issue_no: int
    severity: ReviewIssueSeverity
    category: str | None
    title: str
    description: str
    file_path: str | None
    line_number: int | None
    status: ReviewIssueDecisionStatus
    reason_code: ReviewIssueDecisionReason | None
    note: str | None
    decided_by_id: str | None
    decided_by_name: str | None
    decided_at: datetime | None
    verification_status: ReviewIssueVerificationStatus
    review_head_sha: str | None
    merged_sha: str | None
    batch_status: ReviewBatchStatus
    batch_created_at: datetime
    batch_extracted_at: datetime | None
    batch_verified_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ReviewConsoleIssueListResponse(BaseModel):
    items: list[ReviewConsoleIssueResponse]
    total: int


class ReviewConsoleStatusUpdateRequest(BaseModel):
    status: ReviewIssueDecisionStatus
    reason_code: ReviewIssueDecisionReason | None = None
    note: str | None = Field(default=None, max_length=4000)
    expected_updated_at: datetime

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def validate_decision_evidence(self) -> "ReviewConsoleStatusUpdateRequest":
        if self.status == ReviewIssueDecisionStatus.NOT_ACCEPTED:
            if self.reason_code is None:
                raise ValueError("rejection reason category is required")
            if not self.note:
                raise ValueError("rejection reason detail is required")
        elif self.reason_code is not None:
            raise ValueError("reason_code is only valid when status is not_accepted")
        if self.status == ReviewIssueDecisionStatus.NEEDS_INFO and not self.note:
            raise ValueError("note is required when status is needs_info")
        return self


class ReviewConsoleStatusChangeResponse(BaseModel):
    id: str
    issue_id: str
    previous_status: ReviewIssueDecisionStatus
    new_status: ReviewIssueDecisionStatus
    previous_note: str | None
    new_note: str | None
    previous_reason_code: ReviewIssueDecisionReason | None
    new_reason_code: ReviewIssueDecisionReason | None
    actor_id: str
    actor_name: str
    dimension: Literal["decision", "verification"]
    source: str
    created_at: datetime


class ReviewConsoleStatusChangeListResponse(BaseModel):
    items: list[ReviewConsoleStatusChangeResponse]


class ReviewConsoleStatisticsSummary(BaseModel):
    valid_opinion_total: int
    confirmed_total: int
    false_positive_total: int


class ReviewConsoleContributorStatistics(BaseModel):
    actor_id: str
    actor_name: str
    confirmed_total: int
    accepted_total: int
    not_accepted_total: int


class ReviewConsoleRepositoryStatistics(BaseModel):
    provider: str
    project_path: str
    false_positive_total: int
    rejected_total: int
    decided_total: int
    false_positive_rate: float


class ReviewConsoleStatisticsResponse(BaseModel):
    created_from: datetime | None
    created_to: datetime | None
    summary: ReviewConsoleStatisticsSummary
    contributors: list[ReviewConsoleContributorStatistics]
    top_false_positive_repositories: list[ReviewConsoleRepositoryStatistics]
