from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from cc_fastapi.db.models import TaskStatus


class WorkflowError(RuntimeError):
    pass


class WorkflowNotFoundError(WorkflowError):
    pass


class WorkflowPlanningError(WorkflowError):
    pass


class WorkflowTemplateError(WorkflowPlanningError):
    pass


class WorkflowRetryConflictError(WorkflowError):
    pass


@dataclass(frozen=True)
class WorkflowEvent:
    provider: str
    event_type: str
    payload: dict[str, Any]
    event_uuid: str | None = None
    webhook_uuid: str | None = None
    instance_url: str | None = None
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowTaskSpec:
    prompt: str
    model: str | None = None
    queue_name: str | None = None
    metadata: dict[str, Any] | None = None
    priority: int = 0
    agent_mode: bool = True
    unattended: bool = True
    max_attempts: int | None = None
    claude_agent_options: dict[str, Any] | None = None
    role: str = "primary"


@dataclass(frozen=True)
class WorkflowCorrelationSpec:
    provider: str
    resource_type: str
    project_path: str
    resource_id: str

    def __post_init__(self) -> None:
        normalized = {
            "provider": self.provider.strip().lower(),
            "resource_type": self.resource_type.strip().lower(),
            "project_path": self.project_path.strip(),
            "resource_id": self.resource_id.strip(),
        }
        if not all(normalized.values()):
            raise ValueError("workflow correlation fields must not be empty")
        for field_name, value in normalized.items():
            object.__setattr__(self, field_name, value)


@dataclass(frozen=True)
class WorkflowPlan:
    tasks: tuple[WorkflowTaskSpec, ...] = ()
    skip_reason: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    correlations: tuple[WorkflowCorrelationSpec, ...] = ()
    supersede_correlations: tuple[WorkflowCorrelationSpec, ...] = ()

    @classmethod
    def create_tasks(
        cls,
        *tasks: WorkflowTaskSpec,
        context: dict[str, Any] | None = None,
        correlations: tuple[WorkflowCorrelationSpec, ...] = (),
        supersede_correlations: tuple[WorkflowCorrelationSpec, ...] = (),
    ) -> "WorkflowPlan":
        if not tasks:
            raise ValueError("workflow plan must contain at least one task")
        return cls(
            tasks=tuple(tasks),
            context=context or {},
            correlations=correlations,
            supersede_correlations=supersede_correlations,
        )

    @classmethod
    def skip(
        cls,
        reason: str,
        *,
        context: dict[str, Any] | None = None,
        correlations: tuple[WorkflowCorrelationSpec, ...] = (),
        supersede_correlations: tuple[WorkflowCorrelationSpec, ...] = (),
    ) -> "WorkflowPlan":
        if not reason.strip():
            raise ValueError("workflow skip reason must not be empty")
        return cls(
            skip_reason=reason.strip(),
            context=context or {},
            correlations=correlations,
            supersede_correlations=supersede_correlations,
        )


@dataclass(frozen=True)
class WorkflowTaskOutcome:
    task_id: str
    status: TaskStatus
    result: dict[str, Any] | None
    error_message: str | None


@dataclass(frozen=True)
class WorkflowPostResult:
    context_updates: dict[str, Any] = field(default_factory=dict)


class Workflow(ABC):
    name: str
    version: str = "1"
    priority: int = 0

    @abstractmethod
    def matches(self, event: WorkflowEvent) -> bool:
        raise NotImplementedError

    @abstractmethod
    def before(self, event: WorkflowEvent) -> WorkflowPlan:
        raise NotImplementedError

    def after_task(
        self,
        event: WorkflowEvent,
        outcome: WorkflowTaskOutcome,
        context: dict[str, Any],
    ) -> WorkflowPostResult:
        return WorkflowPostResult()
