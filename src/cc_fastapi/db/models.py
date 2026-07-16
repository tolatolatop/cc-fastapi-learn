from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text, UniqueConstraint
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
