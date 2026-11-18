from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cc_fastapi.core.config import get_settings
from cc_fastapi.hooks.base import resolve_path
from cc_fastapi.services.comment_publisher import CommentTarget, DEFAULT_COMMENT_MAX_CHARS

HOOK_NAME = "review_report"
DEFAULT_REPORT_PATH = "reports/review-report-{task_id}.md"
DEFAULT_MAX_CONTINUES = 3


@dataclass(frozen=True)
class ReviewReportHookConfig:
    enabled: bool
    report_path: Path
    max_continues: int = DEFAULT_MAX_CONTINUES
    require_fresh: bool = True
    publish_to_comment: bool = True
    require_comment_target: bool = False
    comment_target: CommentTarget | None = None
    comment_max_chars: int = DEFAULT_COMMENT_MAX_CHARS


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
    if isinstance(value, int):
        return bool(value)
    return default


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _positive_int(value: Any, default: int, *, minimum: int = 0, maximum: int = 10) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, normalized))


def _extract_hook_entry(metadata: dict[str, Any]) -> Any:
    hooks = metadata.get("hooks")
    if isinstance(hooks, dict) and HOOK_NAME in hooks:
        return hooks[HOOK_NAME]
    # Backwards-friendly aliases for callers that do not nest under "hooks".
    if HOOK_NAME in metadata:
        return metadata[HOOK_NAME]
    if "review_report_hook" in metadata:
        return metadata["review_report_hook"]
    return None


def _provider_name(metadata: dict[str, Any], explicit: Any) -> str | None:
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip().casefold()
    trigger = metadata.get("trigger")
    if isinstance(trigger, str):
        normalized = trigger.strip().casefold()
        if normalized.endswith("_webhook"):
            return normalized.removesuffix("_webhook")
    for provider in ("gitlab", "github"):
        if isinstance(metadata.get(provider), dict):
            return provider
    return None


def _comment_target(
    metadata: dict[str, Any], entry: dict[str, Any]
) -> CommentTarget | None:
    comment = entry.get("comment")
    if isinstance(comment, bool) and not comment:
        return None
    source = comment if isinstance(comment, dict) else entry
    provider = _provider_name(metadata, source.get("provider"))
    if provider is None:
        return None
    provider_block = metadata.get(provider)
    if not isinstance(provider_block, dict):
        provider_block = {}
    project_path = _first_text(
        source.get("project_path"),
        provider_block.get("project_path"),
    )
    number = _first_text(
        source.get("number"),
        source.get("pr_number"),
        source.get("pull_request"),
        source.get("change_request_number"),
        provider_block.get("change_request_number"),
        provider_block.get("pr_number"),
        provider_block.get("pull_request_number"),
    )
    if project_path is None or number is None:
        return None
    instance_url = _first_text(
        source.get("instance_url"),
        provider_block.get("instance_url"),
    )
    return CommentTarget(
        provider=provider,
        project_path=project_path,
        number=number,
        instance_url=instance_url,
    )


def resolve_review_report_hook(
    metadata: dict[str, Any] | None,
    *,
    task_id: str,
    base_cwd: str | Path | None = None,
) -> ReviewReportHookConfig:
    """Resolve the review-report stop hook from one task's metadata.

    Disabled by default. Any of these forms enables it::

        {"hooks": {"review_report": true}}
        {"hooks": {"review_report": {"enabled": true, ...}}}
        {"review_report_hook": {"enabled": true, ...}}
        {"review_report": {"enabled": true, ...}}
    """
    empty = ReviewReportHookConfig(
        enabled=False,
        report_path=Path(DEFAULT_REPORT_PATH.replace("{task_id}", task_id)),
    )
    if not isinstance(metadata, dict):
        return empty

    raw_entry = _extract_hook_entry(metadata)
    if raw_entry is None:
        return empty
    if isinstance(raw_entry, bool):
        if not raw_entry:
            return empty
        entry: dict[str, Any] = {}
    elif isinstance(raw_entry, str):
        if not _as_bool(raw_entry, False):
            return empty
        entry = {}
    elif isinstance(raw_entry, dict):
        if not _as_bool(raw_entry.get("enabled"), True):
            return empty
        entry = raw_entry
    else:
        return empty

    raw_path = _first_text(entry.get("report_path"), entry.get("path")) or DEFAULT_REPORT_PATH
    rendered_path = raw_path.replace("{task_id}", task_id)
    cwd = base_cwd if base_cwd is not None else get_settings().claude_cwd
    report_path = resolve_path(cwd, rendered_path)

    publish_value = entry.get("publish_to_comment")
    if publish_value is None:
        publish_value = entry.get("upload_to_comment")
    publish_to_comment = _as_bool(publish_value, True)

    return ReviewReportHookConfig(
        enabled=True,
        report_path=report_path,
        max_continues=_positive_int(
            entry.get("max_continues", entry.get("max_retries")),
            DEFAULT_MAX_CONTINUES,
            maximum=10,
        ),
        require_fresh=_as_bool(entry.get("require_fresh"), True),
        publish_to_comment=publish_to_comment,
        require_comment_target=_as_bool(entry.get("require_comment_target"), False),
        comment_target=_comment_target(metadata, entry),
        comment_max_chars=_positive_int(
            entry.get("comment_max_chars"),
            DEFAULT_COMMENT_MAX_CHARS,
            minimum=1_000,
            maximum=DEFAULT_COMMENT_MAX_CHARS,
        ),
    )


def describe_review_report_hook(
    metadata: dict[str, Any] | None,
    *,
    task_id: str,
    base_cwd: str | Path | None = None,
) -> str:
    """Return the agent-facing report contract, or an empty string."""
    config = resolve_review_report_hook(metadata, task_id=task_id, base_cwd=base_cwd)
    if not config.enabled:
        return ""
    lines = [
        "Task stop hook `review_report` is enabled.",
        f"Before you finish, write the complete review report to this exact file: {config.report_path}",
        "The file must exist, be non-empty, and be written during this run. "
        "The task is continued automatically if the file is missing; it is not allowed to stop before then.",
    ]
    if config.publish_to_comment:
        lines.append("After the file passes the check, the service publishes it to the PR/MR comment area.")
    return "\n".join(lines)
