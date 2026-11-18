from __future__ import annotations

import logging
import os
from dataclasses import asdict
from datetime import datetime
from typing import Any

from cc_fastapi.core.config import get_settings
from cc_fastapi.hooks.base import (
    HookEvaluation,
    HookOutcome,
    StopHookContext,
)
from cc_fastapi.hooks.config import HOOK_NAME, resolve_review_report_hook
from cc_fastapi.hooks.review_report import ReviewReportHook
from cc_fastapi.services.comment_publisher import CommentPublisher

logger = logging.getLogger(__name__)


def _report_base_cwd(payload: dict[str, Any]) -> str:
    options = payload.get("claude_agent_options")
    if isinstance(options, dict):
        raw_cwd = options.get("cwd")
        if isinstance(raw_cwd, (str, os.PathLike)) and os.fspath(raw_cwd).strip():
            return os.fspath(raw_cwd).strip()
    return get_settings().claude_cwd


def _continue_count(payload: dict[str, Any]) -> int:
    state = payload.get("hook_state")
    if not isinstance(state, dict):
        return 0
    hook_state = state.get(HOOK_NAME)
    if not isinstance(hook_state, dict):
        return 0
    try:
        return max(0, int(hook_state.get("continue_count", 0)))
    except (TypeError, ValueError):
        return 0


class TaskStopHookEngine:
    """Evaluate metadata-controlled stop hooks before a task is marked succeeded."""

    def __init__(self, publisher: CommentPublisher | None = None) -> None:
        self.publisher = publisher or CommentPublisher()

    def evaluate(
        self,
        *,
        task_id: str,
        session_id: str | None,
        started_at: datetime | None,
        metadata: dict[str, Any] | None,
        payload: dict[str, Any] | None,
        output_text: str,
    ) -> HookEvaluation:
        safe_metadata = metadata if isinstance(metadata, dict) else {}
        safe_payload = payload if isinstance(payload, dict) else {}
        config = resolve_review_report_hook(
            safe_metadata,
            task_id=task_id,
            base_cwd=_report_base_cwd(safe_payload),
        )
        if not config.enabled:
            return HookEvaluation.allow(details={"enabled": False})

        context = StopHookContext(
            task_id=task_id,
            session_id=session_id,
            started_at=started_at,
            metadata=safe_metadata,
            payload=safe_payload,
            output_text=output_text,
            continue_count=_continue_count(safe_payload),
        )
        hook = ReviewReportHook(config, publisher=self.publisher)
        try:
            outcome = hook.check(context)
        except Exception as exc:  # A broken hook must not leave the task RUNNING forever.
            logger.exception(
                "task stop hook crashed",
                extra={
                    "event_type": "task_stop_hook_crashed",
                    "task_id": task_id,
                    "reason": f"{type(exc).__name__}: {exc}",
                },
            )
            outcome = HookOutcome.reject(
                f"Stop hook `{HOOK_NAME}` crashed: {type(exc).__name__}: {exc}",
                retryable=False,
            )

        details = {
            "enabled": True,
            "hook": HOOK_NAME,
            "report_path": str(config.report_path),
            "continue_count": context.continue_count,
            "max_continues": config.max_continues,
            **outcome.details,
        }
        if outcome.passed:
            return HookEvaluation(
                passed=True,
                enabled=True,
                hook_name=HOOK_NAME,
                details=details,
            )
        return HookEvaluation(
            passed=False,
            enabled=True,
            hook_name=HOOK_NAME,
            feedback=outcome.feedback,
            retryable=outcome.retryable,
            continue_count=context.continue_count,
            max_continues=config.max_continues,
            details=details,
        )


def build_hook_continuation_prompt(feedback: str) -> str:
    return (
        "A task stop hook blocked the previous stop. Treat the existing conversation as the "
        "source of truth; verify what is already done and do not repeat completed side-effecting "
        "operations. Complete the remaining work and satisfy this requirement before stopping "
        f"again:\n{feedback.strip()}"
    )


def hook_evaluation_payload(evaluation: HookEvaluation) -> dict[str, Any]:
    payload = asdict(evaluation)
    payload.pop("passed", None)
    details = payload.pop("details", {})
    if isinstance(details, dict):
        payload.update(details)
    return payload


__all__ = [
    "TaskStopHookEngine",
    "build_hook_continuation_prompt",
    "hook_evaluation_payload",
]
