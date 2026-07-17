from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import JSON as MySQLJSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


class WorkflowRunStatus(StrEnum):
    PLANNING = "planning"
    RUNNING = "running"
    SKIPPED = "skipped"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class WorkflowStepStatus(StrEnum):
    RUNNING = "running"
    SKIPPED = "skipped"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ReviewBatchStatus(StrEnum):
    COLLECTING = "collecting"
    WAITING_MERGE = "waiting_merge"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReviewIssueSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class ReviewIssueVerificationStatus(StrEnum):
    UNVERIFIED = "unverified"
    ACCEPTED = "accepted"
    NOT_ACCEPTED = "not_accepted"


class AgentTask(Base):
    __tablename__ = "agent_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, native_enum=False, length=32), nullable=False, default=TaskStatus.QUEUED
    )
    queue_name: Mapped[str] = mapped_column(String(64), nullable=False, default="default", index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[dict] = mapped_column(MySQLJSON().with_variant(JSON, "sqlite"), nullable=False, default=dict)
    result: Mapped[dict | None] = mapped_column(MySQLJSON().with_variant(JSON, "sqlite"), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(
        "metadata", MySQLJSON().with_variant(JSON, "sqlite"), nullable=True
    )
    agent_mode: Mapped[bool] = mapped_column(nullable=False, default=True)
    unattended: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    abandoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    abandoned_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    queue_expire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    running_expire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    logs: Mapped[list["AgentTaskLog"]] = relationship("AgentTaskLog", back_populates="task")
    context: Mapped["AgentTaskContext | None"] = relationship("AgentTaskContext", back_populates="task")


class AgentTaskLog(Base):
    __tablename__ = "agent_task_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_tasks.id"), nullable=False, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict | None] = mapped_column(
        "metadata", MySQLJSON().with_variant(JSON, "sqlite"), nullable=True
    )

    task: Mapped[AgentTask] = relationship("AgentTask", back_populates="logs")


class AgentTaskContext(Base):
    __tablename__ = "agent_task_contexts"

    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_tasks.id"), primary_key=True, nullable=False, index=True
    )
    # Keep the physical column name as latest_message for backward compatibility,
    # but store a JSON list with full streamed messages.
    messages_json: Mapped[list[str]] = mapped_column(
        "latest_message", MySQLJSON().with_variant(JSON, "sqlite"), nullable=False, default=list
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    task: Mapped[AgentTask] = relationship("AgentTask", back_populates="context")


class AgentTaskRetryLink(Base):
    __tablename__ = "agent_task_retry_links"

    original_task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_tasks.id"), primary_key=True, nullable=False
    )
    retried_task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_tasks.id"), nullable=False, unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class WebhookTrigger(Base):
    __tablename__ = "webhook_triggers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    event_uuid: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    webhook_uuid: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    instance_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("agent_tasks.id"), nullable=True, index=True)
    payload_json: Mapped[dict] = mapped_column(
        "payload", MySQLJSON().with_variant(JSON, "sqlite"), nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)


class WebhookDeduplicationKey(Base):
    __tablename__ = "webhook_deduplication_keys"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    webhook_uuid: Mapped[str] = mapped_column(String(128), primary_key=True)
    webhook_trigger_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("webhook_triggers.id"),
        nullable=False,
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    workflow_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    workflow_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1")
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_uuid: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    webhook_uuid: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    instance_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict] = mapped_column(
        "payload", MySQLJSON().with_variant(JSON, "sqlite"), nullable=False, default=dict
    )
    config_json: Mapped[dict] = mapped_column(
        "config", MySQLJSON().with_variant(JSON, "sqlite"), nullable=False, default=dict
    )
    context_json: Mapped[dict] = mapped_column(
        "context", MySQLJSON().with_variant(JSON, "sqlite"), nullable=False, default=dict
    )
    status: Mapped[WorkflowRunStatus] = mapped_column(
        Enum(WorkflowRunStatus, native_enum=False, length=32), nullable=False, default=WorkflowRunStatus.PLANNING
    )
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    webhook_trigger_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("webhook_triggers.id"),
        nullable=True,
        unique=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkflowStepRun(Base):
    __tablename__ = "workflow_step_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.id"), nullable=False, index=True
    )
    step_name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[WorkflowStepStatus] = mapped_column(
        Enum(WorkflowStepStatus, native_enum=False, length=32),
        nullable=False,
        default=WorkflowStepStatus.RUNNING,
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    input_json: Mapped[dict | None] = mapped_column(
        "input", MySQLJSON().with_variant(JSON, "sqlite"), nullable=True
    )
    output_json: Mapped[dict | None] = mapped_column(
        "output", MySQLJSON().with_variant(JSON, "sqlite"), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkflowTaskLink(Base):
    __tablename__ = "workflow_task_links"

    workflow_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.id"), primary_key=True, nullable=False
    )
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_tasks.id"), primary_key=True, nullable=False, unique=True, index=True
    )
    role: Mapped[str] = mapped_column(String(64), nullable=False, default="primary")
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class WorkflowCorrelation(Base):
    __tablename__ = "workflow_correlations"
    __table_args__ = (
        UniqueConstraint(
            "workflow_run_id",
            "provider",
            "resource_type",
            "project_path",
            "resource_id",
            name="uq_workflow_correlation_run_resource",
        ),
        Index(
            "ix_workflow_correlation_lookup",
            "provider",
            "resource_type",
            "project_path",
            "resource_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.id"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    project_path: Mapped[str] = mapped_column(String(255), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class WorkflowResourceLock(Base):
    """Persistent lock row used to serialize workflows for one external resource."""

    __tablename__ = "workflow_resource_locks"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    resource_type: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_path: Mapped[str] = mapped_column(String(255), primary_key=True)
    resource_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ReviewIssueBatch(Base):
    """One collection and post-merge verification pass for a review task."""

    __tablename__ = "review_issue_batches"
    __table_args__ = (
        CheckConstraint("issue_count >= 0", name="ck_review_issue_batches_issue_count_nonnegative"),
        Index(
            "ix_review_issue_batches_pull_request",
            "provider",
            "project_path",
            "pr_number",
            "created_at",
        ),
        Index("ix_review_issue_batches_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    instance_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    project_path: Mapped[str] = mapped_column(String(255), nullable=False)
    pr_number: Mapped[str] = mapped_column(String(128), nullable=False)
    pr_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_workflow_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workflow_runs.id"), nullable=True, index=True
    )
    review_task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_tasks.id"), nullable=False, unique=True, index=True
    )
    extract_task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_tasks.id"), nullable=True, unique=True, index=True
    )
    verify_task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_tasks.id"), nullable=True, unique=True, index=True
    )
    review_head_sha: Mapped[str | None] = mapped_column(String(128), nullable=True)
    merged_sha: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[ReviewBatchStatus] = mapped_column(
        Enum(ReviewBatchStatus, native_enum=False, length=32),
        nullable=False,
        default=ReviewBatchStatus.COLLECTING,
    )
    issue_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    issues: Mapped[list["ReviewIssue"]] = relationship("ReviewIssue", back_populates="batch")


class ReviewIssue(Base):
    """A structured issue extracted from an agent's pull-request review."""

    __tablename__ = "review_issues"
    __table_args__ = (
        UniqueConstraint("batch_id", "issue_no", name="uq_review_issues_batch_issue_no"),
        CheckConstraint("issue_no > 0", name="ck_review_issues_issue_no_positive"),
        CheckConstraint(
            "line_number IS NULL OR line_number > 0",
            name="ck_review_issues_line_number_positive",
        ),
        Index(
            "ix_review_issues_statistics",
            "verification_status",
            "severity",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    batch_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("review_issue_batches.id"), nullable=False, index=True
    )
    issue_no: Mapped[int] = mapped_column(Integer, nullable=False)
    severity: Mapped[ReviewIssueSeverity] = mapped_column(
        Enum(ReviewIssueSeverity, native_enum=False, length=32), nullable=False
    )
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    line_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verification_status: Mapped[ReviewIssueVerificationStatus] = mapped_column(
        Enum(ReviewIssueVerificationStatus, native_enum=False, length=32),
        nullable=False,
        default=ReviewIssueVerificationStatus.UNVERIFIED,
    )
    verification_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    batch: Mapped[ReviewIssueBatch] = relationship("ReviewIssueBatch", back_populates="issues")


class Repository(Base):
    """A manually managed platform/repository catalog entry."""

    __tablename__ = "repositories"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "project_path",
            name="uq_repositories_provider_project_path",
        ),
        Index("ix_repositories_updated", "updated_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    project_path: Mapped[str] = mapped_column(String(255), nullable=False)
    tags: Mapped[list[str]] = mapped_column(
        MySQLJSON().with_variant(JSON, "sqlite"), nullable=False, default=list
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
