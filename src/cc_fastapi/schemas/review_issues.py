from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from cc_fastapi.db.models import (
    ReviewBatchStatus,
    ReviewIssueSeverity,
    ReviewIssueVerificationStatus,
    TaskStatus,
)
from cc_fastapi.core.repository_values import (
    normalize_repository_project_path,
    normalize_repository_provider,
)


def _strip_required(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("value must not be blank")
    return normalized


def _strip_optional(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


class ReviewIssueBatchCreateRequest(BaseModel):
    provider: str = Field(max_length=32)
    instance_url: str | None = None
    project_path: str = Field(max_length=255)
    pr_number: str = Field(max_length=128)
    pr_url: str | None = None
    review_workflow_run_id: str | None = Field(default=None, max_length=36)
    review_task_id: str = Field(max_length=36)
    extract_task_id: str | None = Field(default=None, max_length=36)
    verify_task_id: str | None = Field(default=None, max_length=36)
    review_head_sha: str | None = Field(default=None, max_length=128)

    _normalize_required = field_validator("pr_number", "review_task_id")(
        _strip_required
    )
    _normalize_optional = field_validator(
        "instance_url",
        "pr_url",
        "review_workflow_run_id",
        "extract_task_id",
        "verify_task_id",
        "review_head_sha",
    )(_strip_optional)

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        return normalize_repository_provider(value)

    @field_validator("project_path")
    @classmethod
    def normalize_project_path(cls, value: str) -> str:
        return normalize_repository_project_path(value)


class ReviewIssueBatchUpdateRequest(BaseModel):
    status: ReviewBatchStatus | None = None
    extract_task_id: str | None = Field(default=None, max_length=36)
    verify_task_id: str | None = Field(default=None, max_length=36)
    merged_sha: str | None = Field(default=None, max_length=128)
    error_message: str | None = None

    _normalize_optional = field_validator(
        "extract_task_id", "verify_task_id", "merged_sha", "error_message"
    )(_strip_optional)

    @model_validator(mode="after")
    def require_at_least_one_field(self) -> "ReviewIssueBatchUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("status must not be null")
        return self


class ReviewIssueBatchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    provider: str
    instance_url: str | None
    project_path: str
    pr_number: str
    pr_url: str | None
    review_workflow_run_id: str | None
    review_task_id: str
    extract_task_id: str | None
    verify_task_id: str | None
    review_head_sha: str | None
    merged_sha: str | None
    status: ReviewBatchStatus
    issue_count: int
    error_message: str | None
    created_at: datetime
    extracted_at: datetime | None
    verified_at: datetime | None
    updated_at: datetime

    @computed_field
    @property
    def source_type(self) -> Literal["agent_task", "standalone"]:
        if (
            self.status == ReviewBatchStatus.COMPLETED
            and self.review_workflow_run_id is None
            and self.verified_at is None
        ):
            return "standalone"
        return "agent_task"


class ReviewIssueBatchListResponse(BaseModel):
    items: list[ReviewIssueBatchResponse]
    total: int


class ReviewIssueCreateRequest(BaseModel):
    severity: ReviewIssueSeverity
    category: str | None = Field(default=None, max_length=64)
    title: str = Field(max_length=512)
    description: str
    file_path: str | None = Field(default=None, max_length=1024)
    line_number: int | None = Field(default=None, ge=1)

    _normalize_required = field_validator("title", "description")(_strip_required)
    _normalize_optional = field_validator("category", "file_path")(_strip_optional)


class ReviewIssueBulkCreateRequest(BaseModel):
    items: list[ReviewIssueCreateRequest] = Field(max_length=500)


class ReviewPullRequestIssueCreateRequest(BaseModel):
    provider: str = Field(max_length=32)
    project_path: str = Field(max_length=255)
    pr_number: str = Field(max_length=128)
    issues: list[ReviewIssueCreateRequest] = Field(min_length=1, max_length=500)

    _normalize_pr_number = field_validator("pr_number")(_strip_required)

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        return normalize_repository_provider(value)

    @field_validator("project_path")
    @classmethod
    def normalize_project_path(cls, value: str) -> str:
        return normalize_repository_project_path(value)


class ReviewIssueVerificationItemRequest(BaseModel):
    id: str = Field(max_length=36)
    status: ReviewIssueVerificationStatus
    note: str | None = None

    _normalize_id = field_validator("id")(_strip_required)
    _normalize_note = field_validator("note")(_strip_optional)

    @model_validator(mode="after")
    def reject_unverified_status(self) -> "ReviewIssueVerificationItemRequest":
        if self.status == ReviewIssueVerificationStatus.UNVERIFIED:
            raise ValueError("verification result must be accepted or not_accepted")
        return self


class ReviewIssueBulkVerificationRequest(BaseModel):
    items: list[ReviewIssueVerificationItemRequest] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def reject_duplicate_issue_ids(self) -> "ReviewIssueBulkVerificationRequest":
        issue_ids = [item.id for item in self.items]
        if len(issue_ids) != len(set(issue_ids)):
            raise ValueError("verification items contain duplicate issue ids")
        return self


class ReviewIssueVerificationUpdateRequest(BaseModel):
    status: ReviewIssueVerificationStatus
    note: str | None = None

    _normalize_note = field_validator("note")(_strip_optional)

    @model_validator(mode="after")
    def reject_unverified_status(self) -> "ReviewIssueVerificationUpdateRequest":
        if self.status == ReviewIssueVerificationStatus.UNVERIFIED:
            raise ValueError("verification result must be accepted or not_accepted")
        return self


class ReviewIssueResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    batch_id: str
    issue_no: int
    severity: ReviewIssueSeverity
    category: str | None
    title: str
    description: str
    file_path: str | None
    line_number: int | None
    verification_status: ReviewIssueVerificationStatus
    verification_note: str | None
    created_at: datetime
    verified_at: datetime | None
    updated_at: datetime


class ReviewIssueListResponse(BaseModel):
    items: list[ReviewIssueResponse]
    total: int


class ReviewIssueStatisticsResponse(BaseModel):
    batch_total: int
    zero_issue_batches: int
    batch_status_counts: dict[ReviewBatchStatus, int]
    issue_total: int
    verified_issues: int
    accepted_issues: int
    acceptance_rate: float | None
    verification_status_counts: dict[ReviewIssueVerificationStatus, int]
    severity_counts: dict[ReviewIssueSeverity, int]


class ReviewPullRequestReferenceResponse(BaseModel):
    provider: str
    project_path: str
    pr_number: str
    pr_url: str | None


class ReviewIssueTaskReferenceResponse(BaseModel):
    id: str
    status: TaskStatus | None
    session_id: str | None


class ReviewPullRequestIssueItemResponse(ReviewIssueResponse):
    batch_status: ReviewBatchStatus
    review_head_sha: str | None
    merged_sha: str | None
    review_workflow_run_id: str | None
    batch_created_at: datetime
    batch_extracted_at: datetime | None
    batch_verified_at: datetime | None
    batch_error_message: str | None
    source_type: Literal["agent_task", "standalone"]
    review_task: ReviewIssueTaskReferenceResponse | None
    extract_task: ReviewIssueTaskReferenceResponse | None
    verify_task: ReviewIssueTaskReferenceResponse | None


class ReviewPullRequestIssueSummaryResponse(BaseModel):
    batch_total: int
    issue_total: int
    batch_status_counts: dict[ReviewBatchStatus, int]
    verification_status_counts: dict[ReviewIssueVerificationStatus, int]
    severity_counts: dict[ReviewIssueSeverity, int]


class ReviewPullRequestIssueListResponse(BaseModel):
    pull_request: ReviewPullRequestReferenceResponse
    items: list[ReviewPullRequestIssueItemResponse]
    total: int
    summary: ReviewPullRequestIssueSummaryResponse


class ReviewPullRequestIssueCreateResponse(BaseModel):
    pull_request: ReviewPullRequestReferenceResponse
    items: list[ReviewIssueResponse]
    total: int
    idempotent: bool
