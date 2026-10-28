#!/usr/bin/env python3
"""Standalone, provider-neutral pull-request review adapter CLI.

Only GitLab is implemented.  Add another ``ReviewPlatform`` implementation and
register it in ``PlatformFactory`` to support another provider.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import stat
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Self
from urllib.parse import quote, urlsplit

import httpx


class PlatformError(RuntimeError):
    """A safe, user-facing platform operation error."""


class PlatformInputError(PlatformError):
    """The caller supplied invalid input."""


@dataclass(frozen=True, slots=True)
class PullRequestRef:
    provider: str
    instance_url: str
    project_path: str
    number: int
    url: str


@dataclass(frozen=True, slots=True)
class PullRequestSnapshot:
    ref: PullRequestRef
    title: str
    state: str
    source_branch: str
    target_branch: str
    base_sha: str
    start_sha: str
    head_sha: str


@dataclass(frozen=True, slots=True)
class ReviewThread:
    id: str
    resolved: bool
    resolvable: bool
    author: str | None
    file_path: str | None
    line: int | None
    side: str | None
    created_at: str | None
    updated_at: str | None
    notes: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[..., CommandResult | subprocess.CompletedProcess[str]]


class ReviewPlatform(ABC):
    """Minimal outbound platform contract exposed to an Agent."""

    provider: str

    @classmethod
    @abstractmethod
    def supports_url(cls, url: str) -> bool:
        """Return whether this adapter recognizes the pull-request URL shape."""

    @abstractmethod
    def parse_pull_request_url(self, url: str) -> PullRequestRef:
        """Parse and canonicalize a complete pull-request URL.

        Implementations should reject malformed or disallowed hosts and return
        the stable provider, instance, project, and pull-request coordinates.
        """

    @abstractmethod
    def get_pull_request(self, ref: PullRequestRef) -> PullRequestSnapshot:
        """Read the current pull-request metadata and exact diff commit SHAs."""

    @abstractmethod
    def prepare_checkout(self, ref: PullRequestRef, destination: Path) -> dict[str, Any]:
        """Clone the repository and detach the workspace at the current head SHA.

        The destination must be absent or empty. The result describes the
        checked-out snapshot and includes its resolved workspace path.
        """

    @abstractmethod
    def list_threads(
        self,
        ref: PullRequestRef,
        *,
        status: str = "all",
        file_path: str | None = None,
        author: str | None = None,
        updated_after: datetime | None = None,
    ) -> list[ReviewThread]:
        """List normalized review threads matching the optional filters.

        ``status`` accepts ``all``, ``open``, or ``resolved``. Implementations
        are responsible for exhausting provider pagination before filtering.
        """

    @abstractmethod
    def get_thread(self, ref: PullRequestRef, thread_id: str) -> ReviewThread:
        """Return one normalized review thread using its provider thread ID."""

    @abstractmethod
    def create_line_thread(
        self,
        ref: PullRequestRef,
        *,
        file_path: str,
        line: int,
        side: str,
        body: str,
        expected_head_sha: str,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a line-level review thread on the expected pull-request head.

        ``side`` is ``old`` or ``new``. A stale ``expected_head_sha`` must be
        rejected. When supplied, ``operation_id`` makes retries idempotent.
        """

    @abstractmethod
    def reply_thread(
        self,
        ref: PullRequestRef,
        thread_id: str,
        body: str,
        *,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Reply to a review thread, optionally with an idempotency key."""

    @abstractmethod
    def resolve_thread(self, ref: PullRequestRef, thread_id: str) -> dict[str, Any]:
        """Resolve a review thread, returning a skipped result if already resolved."""

    @abstractmethod
    def apply_csv(self, ref: PullRequestRef, csv_path: Path) -> dict[str, Any]:
        """Apply create, reply, and resolve operations from a CSV document.

        Implementations should isolate row failures, continue processing, and
        return aggregate counts together with a result for every input row.
        """


def _required_text(value: Any, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise PlatformInputError(f"{name} is required")
    return normalized


def _parse_positive_int(value: Any, name: str) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise PlatformInputError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise PlatformInputError(f"{name} must be a positive integer")
    return parsed


def _parse_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise PlatformInputError(f"invalid ISO-8601 datetime: {value}") from exc


def _operation_marker(operation_id: str) -> str:
    safe = operation_id.strip()
    if not safe or any(char in safe for char in "<>\r\n"):
        raise PlatformInputError("operation_id must be non-empty and cannot contain <, >, or newlines")
    return f"<!-- cc-platform-operation:{safe} -->"


def _body_with_marker(body: str, operation_id: str | None) -> str:
    normalized = _required_text(body, "body")
    return f"{normalized}\n\n{_operation_marker(operation_id)}" if operation_id else normalized


class GitLabPlatform(ReviewPlatform):
    provider = "gitlab"

    def __init__(
        self,
        token: str,
        *,
        timeout: float = 30.0,
        allowed_hosts: Iterable[str] = (),
        client: httpx.Client | None = None,
        command_runner: CommandRunner = subprocess.run,
    ) -> None:
        self.token = _required_text(token, "GitLab token")
        self.allowed_hosts = {host.strip().casefold() for host in allowed_hosts if host.strip()}
        self.client = client or httpx.Client(
            headers={"PRIVATE-TOKEN": self.token, "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=False,
        )
        self._owns_client = client is None
        self.command_runner = command_runner

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @classmethod
    def supports_url(cls, url: str) -> bool:
        parsed = urlsplit(url.strip())
        return bool(
            parsed.scheme in {"http", "https"}
            and parsed.netloc
            and "/-/merge_requests/" in parsed.path
        )

    def _check_host(self, host: str) -> None:
        if self.allowed_hosts and host.casefold() not in self.allowed_hosts:
            raise PlatformInputError(f"GitLab host is not allowed: {host}")

    def parse_pull_request_url(self, url: str) -> PullRequestRef:
        normalized = _required_text(url, "pull request URL")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise PlatformInputError("pull request URL must be an absolute HTTP or HTTPS URL")
        self._check_host(parsed.hostname or parsed.netloc)
        marker = "/-/merge_requests/"
        if marker not in parsed.path:
            raise PlatformInputError("URL is not a GitLab merge request URL")
        project_part, number_part = parsed.path.split(marker, 1)
        project_path = project_part.strip("/")
        number_text = number_part.strip("/").split("/", 1)[0]
        if not project_path:
            raise PlatformInputError("GitLab project path is missing")
        number = _parse_positive_int(number_text, "merge request number")
        instance_url = f"{parsed.scheme}://{parsed.netloc}"
        canonical_url = f"{instance_url}/{project_path}/-/merge_requests/{number}"
        return PullRequestRef(
            provider=self.provider,
            instance_url=instance_url,
            project_path=project_path,
            number=number,
            url=canonical_url,
        )

    @staticmethod
    def _project_id(ref: PullRequestRef) -> str:
        return quote(ref.project_path, safe="")

    def _api_url(self, ref: PullRequestRef, path: str) -> str:
        return f"{ref.instance_url}/api/v4{path}"

    def _request(
        self,
        method: str,
        ref: PullRequestRef,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            response = self.client.request(
                method,
                self._api_url(ref, path),
                params=params,
                data=data,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise PlatformError(f"GitLab request failed: {exc}") from exc
        if response.is_success:
            return response
        try:
            payload = response.json()
            detail = payload.get("message", payload) if isinstance(payload, dict) else payload
        except ValueError:
            detail = response.text[:500]
        raise PlatformError(f"GitLab API returned {response.status_code}: {detail}")

    def _request_json(
        self,
        method: str,
        ref: PullRequestRef,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> Any:
        response = self._request(method, ref, path, params=params, data=data)
        try:
            return response.json()
        except ValueError as exc:
            raise PlatformError("GitLab API returned invalid JSON") from exc

    def _mr_path(self, ref: PullRequestRef, suffix: str = "") -> str:
        return f"/projects/{self._project_id(ref)}/merge_requests/{ref.number}{suffix}"

    def get_pull_request(self, ref: PullRequestRef) -> PullRequestSnapshot:
        payload = self._request_json("GET", ref, self._mr_path(ref))
        if not isinstance(payload, dict):
            raise PlatformError("GitLab merge request response is not an object")
        diff_refs = payload.get("diff_refs")
        if not isinstance(diff_refs, dict) or not all(
            str(diff_refs.get(name) or "").strip() for name in ("base_sha", "start_sha", "head_sha")
        ):
            versions = self._request_json("GET", ref, self._mr_path(ref, "/versions"))
            if not isinstance(versions, list) or not versions or not isinstance(versions[0], dict):
                raise PlatformError("GitLab has not prepared merge request diff refs yet")
            latest = versions[0]
            diff_refs = {
                "base_sha": latest.get("base_commit_sha"),
                "start_sha": latest.get("start_commit_sha"),
                "head_sha": latest.get("head_commit_sha"),
            }
        return PullRequestSnapshot(
            ref=ref,
            title=str(payload.get("title") or ""),
            state=str(payload.get("state") or ""),
            source_branch=_required_text(payload.get("source_branch"), "source_branch"),
            target_branch=_required_text(payload.get("target_branch"), "target_branch"),
            base_sha=_required_text(diff_refs.get("base_sha"), "base_sha"),
            start_sha=_required_text(diff_refs.get("start_sha"), "start_sha"),
            head_sha=_required_text(diff_refs.get("head_sha"), "head_sha"),
        )

    def _run_git(self, args: Sequence[str], *, env: Mapping[str, str]) -> CommandResult | subprocess.CompletedProcess[str]:
        result = self.command_runner(
            list(args),
            env=dict(env),
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            command = " ".join(args[:3])
            detail = (result.stderr or result.stdout or "git command failed").strip()
            raise PlatformError(f"{command} failed: {detail[-1000:]}")
        return result

    def prepare_checkout(self, ref: PullRequestRef, destination: Path) -> dict[str, Any]:
        destination = destination.expanduser().resolve()
        if destination.exists() and (
            not destination.is_dir() or any(destination.iterdir())
        ):
            raise PlatformInputError(f"checkout destination is not empty: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        snapshot = self.get_pull_request(ref)
        project = self._request_json("GET", ref, f"/projects/{self._project_id(ref)}")
        if not isinstance(project, dict):
            raise PlatformError("GitLab project response is not an object")
        clone_url = _required_text(project.get("http_url_to_repo"), "http_url_to_repo")
        clone_parts = urlsplit(clone_url)
        instance_host = urlsplit(ref.instance_url).hostname
        if not clone_parts.hostname or not instance_host:
            raise PlatformError("GitLab returned an invalid clone URL")
        if clone_parts.hostname.casefold() != instance_host.casefold():
            raise PlatformError("GitLab returned a clone URL on a different host")

        askpass_source = """#!/usr/bin/env python3
import os
import sys
prompt = sys.argv[1] if len(sys.argv) > 1 else ""
print("oauth2" if "username" in prompt.casefold() else os.environ["PLATFORM_GIT_TOKEN"])
"""
        with tempfile.TemporaryDirectory(prefix="cc-platform-") as temp_dir:
            askpass = Path(temp_dir) / "askpass.py"
            askpass.write_text(askpass_source, encoding="utf-8")
            askpass.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            git_env = {
                **os.environ,
                "GIT_ASKPASS": str(askpass),
                "GIT_TERMINAL_PROMPT": "0",
                "PLATFORM_GIT_TOKEN": self.token,
            }
            self._run_git(
                ["git", "clone", "--no-checkout", clone_url, str(destination)],
                env=git_env,
            )
            self._run_git(
                [
                    "git",
                    "-C",
                    str(destination),
                    "fetch",
                    "origin",
                    f"refs/merge-requests/{ref.number}/head",
                ],
                env=git_env,
            )
            self._run_git(
                ["git", "-C", str(destination), "checkout", "--detach", snapshot.head_sha],
                env=git_env,
            )
            actual = self._run_git(
                ["git", "-C", str(destination), "rev-parse", "HEAD"],
                env=git_env,
            ).stdout.strip()
        if actual != snapshot.head_sha:
            raise PlatformError(
                f"checked out unexpected HEAD: expected {snapshot.head_sha}, got {actual or '<empty>'}"
            )
        return {
            **asdict(snapshot),
            "ref": asdict(snapshot.ref),
            "workspace_path": str(destination),
        }

    def _list_discussions_raw(self, ref: PullRequestRef) -> list[dict[str, Any]]:
        discussions: list[dict[str, Any]] = []
        page = 1
        while page:
            response = self._request(
                "GET",
                ref,
                self._mr_path(ref, "/discussions"),
                params={"per_page": 100, "page": page},
            )
            try:
                items = response.json()
            except ValueError as exc:
                raise PlatformError("GitLab API returned invalid discussion JSON") from exc
            if not isinstance(items, list):
                raise PlatformError("GitLab discussions response is not a list")
            discussions.extend(item for item in items if isinstance(item, dict))
            next_page = response.headers.get("X-Next-Page", "").strip()
            page = int(next_page) if next_page else 0
        return discussions

    @staticmethod
    def _normalize_thread(payload: Mapping[str, Any]) -> ReviewThread:
        notes = [dict(note) for note in payload.get("notes", []) if isinstance(note, Mapping)]
        root = notes[0] if notes else {}
        resolvable_notes = [note for note in notes if note.get("resolvable") is True]
        position = root.get("position") if isinstance(root.get("position"), Mapping) else {}
        line: int | None = None
        side: str | None = None
        if isinstance(position.get("new_line"), int):
            line, side = position["new_line"], "new"
        elif isinstance(position.get("old_line"), int):
            line, side = position["old_line"], "old"
        author_payload = root.get("author") if isinstance(root.get("author"), Mapping) else {}
        timestamps = [str(note.get("updated_at")) for note in notes if note.get("updated_at")]
        return ReviewThread(
            id=str(payload.get("id") or ""),
            resolved=bool(resolvable_notes) and all(note.get("resolved") is True for note in resolvable_notes),
            resolvable=bool(resolvable_notes),
            author=str(author_payload.get("username") or author_payload.get("name") or "") or None,
            file_path=str(position.get("new_path") or position.get("old_path") or "") or None,
            line=line,
            side=side,
            created_at=str(root.get("created_at") or "") or None,
            updated_at=max(timestamps) if timestamps else None,
            notes=notes,
        )

    def list_threads(
        self,
        ref: PullRequestRef,
        *,
        status: str = "all",
        file_path: str | None = None,
        author: str | None = None,
        updated_after: datetime | None = None,
    ) -> list[ReviewThread]:
        normalized_status = status.strip().casefold()
        if normalized_status not in {"all", "open", "resolved"}:
            raise PlatformInputError("status must be all, open, or resolved")
        threads = [self._normalize_thread(item) for item in self._list_discussions_raw(ref)]
        if normalized_status == "open":
            threads = [thread for thread in threads if thread.resolvable and not thread.resolved]
        elif normalized_status == "resolved":
            threads = [thread for thread in threads if thread.resolved]
        if file_path:
            threads = [thread for thread in threads if thread.file_path == file_path]
        if author:
            expected = author.strip().casefold()
            threads = [thread for thread in threads if (thread.author or "").casefold() == expected]
        if updated_after:
            threads = [
                thread
                for thread in threads
                if thread.updated_at and _parse_datetime(thread.updated_at) > updated_after
            ]
        return threads

    def get_thread(self, ref: PullRequestRef, thread_id: str) -> ReviewThread:
        normalized_id = quote(_required_text(thread_id, "thread_id"), safe="")
        payload = self._request_json(
            "GET", ref, self._mr_path(ref, f"/discussions/{normalized_id}")
        )
        if not isinstance(payload, dict):
            raise PlatformError("GitLab discussion response is not an object")
        return self._normalize_thread(payload)

    def _find_operation(self, ref: PullRequestRef, operation_id: str) -> str | None:
        marker = _operation_marker(operation_id)
        for discussion in self._list_discussions_raw(ref):
            notes = discussion.get("notes")
            if isinstance(notes, list) and any(
                marker in str(note.get("body") or "") for note in notes if isinstance(note, dict)
            ):
                return str(discussion.get("id") or "") or None
        return None

    def create_line_thread(
        self,
        ref: PullRequestRef,
        *,
        file_path: str,
        line: int,
        side: str,
        body: str,
        expected_head_sha: str,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        if operation_id and (existing := self._find_operation(ref, operation_id)):
            return {"status": "skipped", "reason": "operation already applied", "thread_id": existing}
        snapshot = self.get_pull_request(ref)
        expected = _required_text(expected_head_sha, "expected_head_sha")
        if snapshot.head_sha != expected:
            raise PlatformError(
                f"merge request HEAD changed: expected {expected}, current {snapshot.head_sha}"
            )
        normalized_path = _required_text(file_path, "file_path")
        normalized_side = side.strip().casefold()
        if normalized_side not in {"old", "new"}:
            raise PlatformInputError("side must be old or new")
        data: dict[str, Any] = {
            "body": _body_with_marker(body, operation_id),
            "position[position_type]": "text",
            "position[base_sha]": snapshot.base_sha,
            "position[start_sha]": snapshot.start_sha,
            "position[head_sha]": snapshot.head_sha,
            "position[old_path]": normalized_path,
            "position[new_path]": normalized_path,
            f"position[{normalized_side}_line]": _parse_positive_int(line, "line"),
        }
        payload = self._request_json("POST", ref, self._mr_path(ref, "/discussions"), data=data)
        if not isinstance(payload, dict):
            raise PlatformError("GitLab create discussion response is not an object")
        return {"status": "succeeded", "thread_id": str(payload.get("id") or ""), "discussion": payload}

    def reply_thread(
        self,
        ref: PullRequestRef,
        thread_id: str,
        body: str,
        *,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_id = quote(_required_text(thread_id, "thread_id"), safe="")
        if operation_id and (existing := self._find_operation(ref, operation_id)):
            return {"status": "skipped", "reason": "operation already applied", "thread_id": existing}
        payload = self._request_json(
            "POST",
            ref,
            self._mr_path(ref, f"/discussions/{normalized_id}/notes"),
            data={"body": _body_with_marker(body, operation_id)},
        )
        if not isinstance(payload, dict):
            raise PlatformError("GitLab reply response is not an object")
        return {
            "status": "succeeded",
            "thread_id": thread_id,
            "comment_id": payload.get("id"),
            "note": payload,
        }

    def resolve_thread(self, ref: PullRequestRef, thread_id: str) -> dict[str, Any]:
        current = self.get_thread(ref, thread_id)
        if current.resolved:
            return {"status": "skipped", "reason": "thread already resolved", "thread_id": thread_id}
        normalized_id = quote(_required_text(thread_id, "thread_id"), safe="")
        payload = self._request_json(
            "PUT",
            ref,
            self._mr_path(ref, f"/discussions/{normalized_id}"),
            data={"resolved": "true"},
        )
        if not isinstance(payload, dict):
            raise PlatformError("GitLab resolve response is not an object")
        return {"status": "succeeded", "thread_id": thread_id, "discussion": payload}

    @staticmethod
    def _read_csv(csv_path: Path) -> list[dict[str, str]]:
        try:
            stream = sys.stdin if str(csv_path) == "-" else csv_path.open("r", encoding="utf-8-sig", newline="")
        except OSError as exc:
            raise PlatformInputError(f"cannot read CSV: {csv_path}") from exc
        close_stream = stream is not sys.stdin
        try:
            reader = csv.DictReader(stream)
            required_headers = {"operation_id", "action", "thread_id", "file_path", "line", "side", "body", "head_sha"}
            missing = required_headers - set(reader.fieldnames or [])
            if missing:
                raise PlatformInputError(f"CSV is missing columns: {', '.join(sorted(missing))}")
            return [dict(row) for row in reader]
        finally:
            if close_stream:
                stream.close()

    def apply_csv(self, ref: PullRequestRef, csv_path: Path) -> dict[str, Any]:
        rows = self._read_csv(csv_path)
        results: list[dict[str, Any]] = []
        for row_number, row in enumerate(rows, start=2):
            operation_id = str(row.get("operation_id") or "").strip()
            action = str(row.get("action") or "").strip().casefold()
            try:
                _operation_marker(operation_id)
                if action == "create":
                    result = self.create_line_thread(
                        ref,
                        file_path=str(row.get("file_path") or ""),
                        line=_parse_positive_int(row.get("line"), "line"),
                        side=str(row.get("side") or ""),
                        body=str(row.get("body") or ""),
                        expected_head_sha=str(row.get("head_sha") or ""),
                        operation_id=operation_id,
                    )
                elif action == "reply":
                    result = self.reply_thread(
                        ref,
                        str(row.get("thread_id") or ""),
                        str(row.get("body") or ""),
                        operation_id=operation_id,
                    )
                elif action == "resolve":
                    result = self.resolve_thread(ref, str(row.get("thread_id") or ""))
                else:
                    raise PlatformInputError("action must be create, reply, or resolve")
                results.append({"row": row_number, "operation_id": operation_id, "action": action, **result})
            except PlatformError as exc:
                results.append(
                    {
                        "row": row_number,
                        "operation_id": operation_id,
                        "action": action,
                        "status": "failed",
                        "error": str(exc),
                    }
                )
        return {
            "total": len(results),
            "succeeded": sum(item["status"] == "succeeded" for item in results),
            "skipped": sum(item["status"] == "skipped" for item in results),
            "failed": sum(item["status"] == "failed" for item in results),
            "results": results,
        }


class PlatformFactory:
    """Small registration point for future provider implementations."""

    platform_types: tuple[type[ReviewPlatform], ...] = (GitLabPlatform,)

    @classmethod
    def build(
        cls,
        url: str,
        *,
        gitlab_token: str,
        allowed_hosts: Iterable[str] = (),
    ) -> ReviewPlatform:
        for platform_type in cls.platform_types:
            if platform_type.supports_url(url) and platform_type is GitLabPlatform:
                return GitLabPlatform(gitlab_token, allowed_hosts=allowed_hosts)
        raise PlatformInputError("no platform adapter supports this pull request URL")


def _body_argument(args: argparse.Namespace) -> str:
    if getattr(args, "body_file", None):
        if args.body_file == "-":
            return sys.stdin.read()
        try:
            return Path(args.body_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise PlatformInputError(f"cannot read body file: {args.body_file}") from exc
    return _required_text(getattr(args, "body", ""), "body or --body-file")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--token-env",
        default="GITLAB_TOKEN",
        help="environment variable containing the GitLab token (default: GITLAB_TOKEN)",
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="allowed GitLab host; repeat as needed (defaults to PLATFORM_ALLOWED_HOSTS)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    checkout = commands.add_parser("checkout", help="clone and check out an MR head SHA")
    checkout.add_argument("url")
    checkout.add_argument("destination", type=Path)

    list_parser = commands.add_parser("review-list", help="list and filter MR discussions")
    list_parser.add_argument("url")
    list_parser.add_argument("--status", choices=("all", "open", "resolved"), default="all")
    list_parser.add_argument("--file-path")
    list_parser.add_argument("--author")
    list_parser.add_argument("--updated-after", type=_parse_datetime)

    get_parser = commands.add_parser("review-get", help="get one MR discussion")
    get_parser.add_argument("url")
    get_parser.add_argument("thread_id")

    create = commands.add_parser("review-create", help="create a line-level MR discussion")
    create.add_argument("url")
    create.add_argument("--file-path", required=True)
    create.add_argument("--line", type=int, required=True)
    create.add_argument("--side", choices=("old", "new"), required=True)
    create.add_argument("--head-sha", required=True)
    create.add_argument("--body")
    create.add_argument("--body-file")
    create.add_argument("--operation-id")

    reply = commands.add_parser("review-reply", help="reply to an MR discussion")
    reply.add_argument("url")
    reply.add_argument("thread_id")
    reply.add_argument("--body")
    reply.add_argument("--body-file")
    reply.add_argument("--operation-id")

    resolve = commands.add_parser("review-resolve", help="resolve an MR discussion")
    resolve.add_argument("url")
    resolve.add_argument("thread_id")

    batch = commands.add_parser("batch", help="apply create/reply/resolve actions from CSV")
    batch.add_argument("url")
    batch.add_argument("csv_path", type=Path, help="CSV file or - for stdin")
    return parser


def _jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = os.environ.get(args.token_env, "")
    env_hosts = [item.strip() for item in os.environ.get("PLATFORM_ALLOWED_HOSTS", "").split(",") if item.strip()]
    try:
        platform = PlatformFactory.build(
            args.url,
            gitlab_token=token,
            allowed_hosts=args.allowed_host or env_hosts,
        )
        try:
            ref = platform.parse_pull_request_url(args.url)
            if args.command == "checkout":
                result = platform.prepare_checkout(ref, args.destination)
            elif args.command == "review-list":
                result = platform.list_threads(
                    ref,
                    status=args.status,
                    file_path=args.file_path,
                    author=args.author,
                    updated_after=args.updated_after,
                )
            elif args.command == "review-get":
                result = platform.get_thread(ref, args.thread_id)
            elif args.command == "review-create":
                result = platform.create_line_thread(
                    ref,
                    file_path=args.file_path,
                    line=args.line,
                    side=args.side,
                    body=_body_argument(args),
                    expected_head_sha=args.head_sha,
                    operation_id=args.operation_id,
                )
            elif args.command == "review-reply":
                result = platform.reply_thread(
                    ref,
                    args.thread_id,
                    _body_argument(args),
                    operation_id=args.operation_id,
                )
            elif args.command == "review-resolve":
                result = platform.resolve_thread(ref, args.thread_id)
            else:
                result = platform.apply_csv(ref, args.csv_path)
        finally:
            close = getattr(platform, "close", None)
            if callable(close):
                close()
    except PlatformError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    batch_failed = (
        args.command == "batch"
        and isinstance(result, dict)
        and int(result.get("failed", 0)) > 0
    )
    print(
        json.dumps(
            {"ok": not batch_failed, "result": _jsonable(result)},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 3 if batch_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
