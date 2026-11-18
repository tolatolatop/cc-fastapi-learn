from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cc_fastapi.hooks.base import HookOutcome, StopHook, StopHookContext
from cc_fastapi.hooks.config import HOOK_NAME, ReviewReportHookConfig
from cc_fastapi.services.comment_publisher import CommentPublisher, CommentPublishError

FRESHNESS_TOLERANCE_SECONDS = 2.0


def _epoch(value: datetime | None) -> float | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.timestamp()


class ReviewReportHook(StopHook):
    """Gate a stopped agent on a review report file and publish it as a comment."""

    name = HOOK_NAME

    def __init__(
        self,
        config: ReviewReportHookConfig,
        publisher: CommentPublisher | None = None,
    ) -> None:
        self.config = config
        self.publisher = publisher or CommentPublisher()

    def check(self, context: StopHookContext) -> HookOutcome:
        report_path = self.config.report_path
        state = context.hook_state.get(HOOK_NAME)
        state = state if isinstance(state, dict) else {}

        if not report_path.exists():
            return HookOutcome.reject(
                self._missing_feedback(report_path),
                retryable=True,
                report_path=str(report_path),
            )
        if not report_path.is_file():
            return HookOutcome.reject(
                f"Report path is not a regular file: {report_path}. "
                "Replace it with the review report file and then stop.",
                retryable=True,
                report_path=str(report_path),
            )
        try:
            size = report_path.stat().st_size
        except OSError as exc:
            return HookOutcome.reject(
                f"Cannot stat report file {report_path}: {exc}. Rewrite the report file and stop.",
                retryable=True,
                report_path=str(report_path),
            )
        if size <= 0:
            return HookOutcome.reject(
                f"Report file is empty: {report_path}. Write the complete review report and stop.",
                retryable=True,
                report_path=str(report_path),
            )

        if self.config.require_fresh:
            started_epoch = _epoch(context.started_at)
            modified_epoch = _epoch(datetime.fromtimestamp(report_path.stat().st_mtime, tz=UTC))
            if (
                started_epoch is not None
                and modified_epoch is not None
                and modified_epoch + FRESHNESS_TOLERANCE_SECONDS < started_epoch
            ):
                return HookOutcome.reject(
                    f"Report file {report_path} predates this run and was not rewritten. "
                    "Regenerate the complete review report now, then stop.",
                    retryable=True,
                    report_path=str(report_path),
                    size=size,
                )

        if not self.config.publish_to_comment:
            return HookOutcome.allow(
                report_path=str(report_path),
                size=size,
                published=False,
            )

        if state.get("published_comment_id") or state.get("published") is True:
            return HookOutcome.allow(
                report_path=str(report_path),
                size=size,
                published=False,
                already_published=True,
            )

        if self.config.comment_target is None:
            if self.config.require_comment_target:
                return HookOutcome.reject(
                    "review_report hook requires a comment target, but the task metadata has no "
                    "GitLab/GitHub provider, project_path and change-request number. Either run the "
                    "task from a PR/MR webhook, add hooks.review_report.comment, or set "
                    "require_comment_target=false.",
                    retryable=False,
                    report_path=str(report_path),
                )
            return HookOutcome.allow(
                report_path=str(report_path),
                size=size,
                published=False,
                comment_target_missing=True,
                comment_skipped_reason="no_gitlab_or_github_change_request",
            )

        try:
            publication = self.publisher.publish(
                self.config.comment_target,
                task_id=context.task_id,
                session_id=context.session_id,
                report_path=report_path,
                max_chars=self.config.comment_max_chars,
            )
        except CommentPublishError as exc:
            return HookOutcome.reject(
                f"Report file exists but publishing it to the PR/MR comment area failed: {exc}",
                retryable=False,
                report_path=str(report_path),
                size=size,
            )

        return HookOutcome.allow(
            report_path=str(report_path),
            size=size,
            published=True,
            provider=publication.provider,
            comment_id=publication.comment_id,
            comment_url=publication.comment_url,
            comment_truncated=publication.truncated,
        )

    def _missing_feedback(self, report_path: Path) -> str:
        return (
            f"Stop hook `review_report` rejected this run: the review report file does not exist at "
            f"{report_path}. Continue the same task, finish the review, and write the complete report "
            "to that exact path before stopping again."
        )
