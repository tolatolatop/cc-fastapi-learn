from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from cc_fastapi.core.config import get_settings

logger = logging.getLogger(__name__)

MAX_REPORT_BYTES = 2_000_000
DEFAULT_COMMENT_MAX_CHARS = 60_000


class CommentPublishError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommentTarget:
    provider: str
    project_path: str
    number: str
    instance_url: str | None = None

    def normalized(self) -> "CommentTarget":
        provider = self.provider.strip().casefold()
        project_path = self.project_path.strip().strip("/")
        number = self.number.strip()
        instance_url = (self.instance_url or "").strip().rstrip("/") or None
        if not provider:
            raise CommentPublishError("comment target provider is empty")
        if not project_path:
            raise CommentPublishError("comment target project_path is empty")
        if not number:
            raise CommentPublishError("comment target pull/merge request number is empty")
        return CommentTarget(
            provider=provider,
            project_path=project_path,
            number=number,
            instance_url=instance_url,
        )


@dataclass(frozen=True)
class CommentPublication:
    provider: str
    comment_id: str | None = None
    comment_url: str | None = None
    body_chars: int = 0
    truncated: bool = False


def build_report_comment(
    *,
    task_id: str,
    session_id: str | None,
    report_path: Path,
    report_text: str,
    max_chars: int = DEFAULT_COMMENT_MAX_CHARS,
) -> tuple[str, bool]:
    """Render one idempotent PR/MR comment containing the report file."""
    normalized_limit = max(1_000, min(int(max_chars), DEFAULT_COMMENT_MAX_CHARS))
    truncated = len(report_text) > normalized_limit
    visible = report_text[:normalized_limit]
    if truncated:
        visible += "\n\n[报告内容过长，评论中已截断；完整文件见任务工作目录。]"
    published_at = datetime.now(UTC).isoformat()
    body = (
        f"<!-- cc-fastapi:review-report:{task_id} -->\n"
        "### 检视报告\n\n"
        f"- 任务: `{task_id}`\n"
        f"- 会话: `{session_id or '-'}`\n"
        f"- 文件: `{report_path.name}`\n"
        f"- 发布时间: {published_at}\n\n"
        "<details>\n"
        "<summary>展开报告内容</summary>\n\n"
        f"{visible}\n\n"
        "</details>\n"
    )
    return body, truncated


class CommentPublisher:
    """Publish a report file into a GitLab MR or GitHub PR comment thread."""

    def __init__(
        self,
        *,
        settings: Any | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.settings = settings or get_settings()
        self.transport = transport
        self.timeout = timeout

    def publish(
        self,
        target: CommentTarget,
        *,
        task_id: str,
        session_id: str | None,
        report_path: Path,
        max_chars: int = DEFAULT_COMMENT_MAX_CHARS,
    ) -> CommentPublication:
        normalized = target.normalized()
        report_text = self._read_report(report_path)
        body, truncated = build_report_comment(
            task_id=task_id,
            session_id=session_id,
            report_path=report_path,
            report_text=report_text,
            max_chars=max_chars,
        )
        if normalized.provider == "gitlab":
            return self._publish_gitlab(normalized, body, truncated)
        if normalized.provider == "github":
            return self._publish_github(normalized, body, truncated)
        raise CommentPublishError(f"unsupported comment provider: {normalized.provider}")

    @staticmethod
    def _read_report(report_path: Path) -> str:
        try:
            raw = report_path.read_bytes()
        except OSError as exc:
            raise CommentPublishError(f"cannot read report file: {report_path}") from exc
        if len(raw) > MAX_REPORT_BYTES:
            raw = raw[:MAX_REPORT_BYTES]
        return raw.decode("utf-8", errors="replace")

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout, transport=self.transport)

    @staticmethod
    def _raise_for_status(provider: str, response: httpx.Response) -> None:
        if response.is_success:
            return
        detail = response.text.strip()
        if len(detail) > 500:
            detail = detail[:500] + "..."
        raise CommentPublishError(
            f"{provider} comment API returned HTTP {response.status_code}: {detail}"
        )

    def _publish_gitlab(
        self, target: CommentTarget, body: str, truncated: bool
    ) -> CommentPublication:
        token = self.settings.gitlab_api_token.strip()
        if not token:
            raise CommentPublishError("GITLAB_API_TOKEN is required to publish a report comment")
        base_url = (
            self.settings.gitlab_api_url.strip().rstrip("/")
            or target.instance_url
            or "https://gitlab.com"
        )
        project = quote(target.project_path, safe="")
        url = f"{base_url}/api/v4/projects/{project}/merge_requests/{target.number}/notes"
        with self._client() as client:
            response = client.post(
                url,
                headers={"PRIVATE-TOKEN": token},
                json={"body": body},
            )
            self._raise_for_status("gitlab", response)
        payload = response.json() if response.content else {}
        comment_id = str(payload.get("id")) if isinstance(payload, dict) and payload.get("id") else None
        return CommentPublication(
            provider="gitlab",
            comment_id=comment_id,
            comment_url=f"{base_url}/{target.project_path}/-/merge_requests/{target.number}#note_{comment_id}"
            if comment_id
            else None,
            body_chars=len(body),
            truncated=truncated,
        )

    def _publish_github(
        self, target: CommentTarget, body: str, truncated: bool
    ) -> CommentPublication:
        token = self.settings.github_api_token.strip()
        if not token:
            raise CommentPublishError("GITHUB_API_TOKEN is required to publish a report comment")
        base_url = self._github_api_base(target.instance_url)
        url = f"{base_url}/repos/{target.project_path}/issues/{target.number}/comments"
        with self._client() as client:
            response = client.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json={"body": body},
            )
            self._raise_for_status("github", response)
        payload = response.json() if response.content else {}
        comment_id = str(payload.get("id")) if isinstance(payload, dict) and payload.get("id") else None
        return CommentPublication(
            provider="github",
            comment_id=comment_id,
            comment_url=payload.get("html_url") if isinstance(payload, dict) else None,
            body_chars=len(body),
            truncated=truncated,
        )

    def _github_api_base(self, instance_url: str | None) -> str:
        configured = self.settings.github_api_url.strip().rstrip("/")
        if configured:
            return configured
        if not instance_url or instance_url.rstrip("/").casefold() == "https://github.com":
            return "https://api.github.com"
        return f"{instance_url.rstrip('/')}/api/v3"
