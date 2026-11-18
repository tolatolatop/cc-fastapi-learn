from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StopHookContext:
    """Everything a stop hook may inspect after an agent stops normally."""

    task_id: str
    session_id: str | None
    started_at: datetime | None
    metadata: dict[str, Any]
    payload: dict[str, Any]
    output_text: str
    continue_count: int = 0

    @property
    def hook_state(self) -> dict[str, Any]:
        state = self.payload.get("hook_state")
        return state if isinstance(state, dict) else {}


@dataclass(frozen=True)
class HookOutcome:
    """Result of one hook check.

    ``passed`` means the task may stop. A failed but retryable outcome is fed
    back to the agent and the task is continued in the same session. A failed,
    non-retryable outcome fails the task immediately.
    """

    passed: bool
    feedback: str = ""
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def allow(cls, **details: Any) -> "HookOutcome":
        return cls(passed=True, details=details)

    @classmethod
    def reject(cls, feedback: str, *, retryable: bool = True, **details: Any) -> "HookOutcome":
        return cls(
            passed=False,
            feedback=feedback.strip(),
            retryable=retryable,
            details=details,
        )


class StopHook(ABC):
    """A gate evaluated after the agent reports a normal stop."""

    name: str

    @abstractmethod
    def check(self, context: StopHookContext) -> HookOutcome:
        raise NotImplementedError


@dataclass(frozen=True)
class HookEvaluation:
    passed: bool
    enabled: bool = False
    hook_name: str | None = None
    feedback: str = ""
    retryable: bool = False
    continue_count: int = 0
    max_continues: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def allow(cls, *, details: dict[str, Any] | None = None) -> "HookEvaluation":
        return cls(passed=True, details=details or {})

    @property
    def retry_allowed(self) -> bool:
        return self.retryable and self.continue_count < self.max_continues


def resolve_path(base_cwd: str | Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path(base_cwd) / path
    return path.resolve()
