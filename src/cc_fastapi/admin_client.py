from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from cc_fastapi.core.repository_values import (
    normalize_repository_project_path,
    normalize_repository_provider,
)
from cc_fastapi.schemas.review_issues import ReviewIssueBulkCreateRequest


class AdminClientError(RuntimeError):
    exit_code = 6


class AdminInputError(AdminClientError):
    exit_code = 2


class AdminNotFoundError(AdminClientError):
    exit_code = 3


class AdminConflictError(AdminClientError):
    exit_code = 4


class AdminAuthError(AdminClientError):
    exit_code = 5


class VerifyResult(BaseModel):
    issue_no: int = Field(ge=1)
    status: str
    note: str | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in {"accepted", "not_accepted"}:
            raise ValueError("status must be accepted or not_accepted")
        return normalized

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class VerifyInput(BaseModel):
    results: list[VerifyResult] = Field(max_length=500)

    @model_validator(mode="after")
    def reject_duplicates(self) -> "VerifyInput":
        issue_numbers = [item.issue_no for item in self.results]
        if len(issue_numbers) != len(set(issue_numbers)):
            raise ValueError("results contain duplicate issue_no values")
        return self


@dataclass(frozen=True)
class PullRequestIdentity:
    provider: str
    project_path: str
    pr_number: str

    def __post_init__(self) -> None:
        try:
            provider = normalize_repository_provider(self.provider)
            project_path = normalize_repository_project_path(self.project_path)
        except ValueError as exc:
            raise AdminInputError(str(exc)) from exc
        number = self.pr_number.strip()
        if not number:
            raise AdminInputError("pr_number must not be blank")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "project_path", project_path)
        object.__setattr__(self, "pr_number", number)

    def params(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "project_path": self.project_path,
            "pr_number": self.pr_number,
        }


class AdminApiClient:
    def __init__(
        self,
        base_url: str,
        token: str = "",
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        normalized_url = base_url.strip().rstrip("/")
        if not normalized_url:
            raise AdminInputError("CC_FASTAPI_BASE_URL is required")
        headers = {"X-API-Token": token} if token else {}
        self.client = httpx.Client(
            base_url=normalized_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "AdminApiClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        json: Any = None,
    ) -> dict[str, Any]:
        try:
            response = self.client.request(method, path, params=params, json=json)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise AdminClientError(f"cannot connect to API: {exc}") from exc
        payload: Any
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.is_success:
            if not isinstance(payload, dict):
                raise AdminClientError("API returned a non-object JSON response")
            return payload
        detail = (
            payload.get("detail")
            if isinstance(payload, dict) and payload.get("detail")
            else f"API request failed with status {response.status_code}"
        )
        if response.status_code == 401:
            raise AdminAuthError(str(detail))
        if response.status_code == 404:
            raise AdminNotFoundError(str(detail))
        if response.status_code == 409:
            raise AdminConflictError(str(detail))
        if response.status_code == 422:
            raise AdminInputError(str(detail))
        raise AdminClientError(str(detail))

    def paged_items(
        self,
        path: str,
        *,
        params: dict[str, Any],
        limit: int = 200,
    ) -> tuple[list[dict[str, Any]], int]:
        offset = 0
        items: list[dict[str, Any]] = []
        total = 0
        while True:
            page = self.request(
                "GET",
                path,
                params={**params, "offset": offset, "limit": limit},
            )
            page_items = page.get("items", [])
            if not isinstance(page_items, list):
                raise AdminClientError("API page is missing items")
            items.extend(item for item in page_items if isinstance(item, dict))
            total = int(page.get("total", len(items)))
            if not page_items or len(items) >= total:
                return items, total
            offset += len(page_items)

    def recent(
        self,
        *,
        provider: str | None,
        project_path: str | None,
        states: list[str],
        search: str | None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        params: list[tuple[str, Any]] = [("offset", offset), ("limit", limit)]
        if provider:
            params.append(("provider", provider))
        if project_path:
            params.append(("project_path", project_path))
        if search:
            params.append(("q", search))
        params.extend(("state", state) for state in states)
        return self.request("GET", "/v1/internal/change-requests", params=params)

    def detail(
        self,
        identity: PullRequestIdentity,
        *,
        task_id: str | None = None,
        task_statuses: list[str] | None = None,
        include_result: bool = True,
        all_tasks: bool = True,
    ) -> dict[str, Any]:
        base_params: list[tuple[str, Any]] = list(identity.params().items())
        if task_id:
            base_params.append(("task_id", task_id))
        for task_status in task_statuses or []:
            base_params.append(("task_status", task_status))
        base_params.append(("include_result", str(include_result).lower()))
        offset = 0
        tasks: list[dict[str, Any]] = []
        detail: dict[str, Any] | None = None
        while True:
            page = self.request(
                "GET",
                "/v1/internal/change-requests/detail",
                params=[
                    *base_params,
                    ("task_offset", offset),
                    ("task_limit", 500),
                ],
            )
            if detail is None:
                detail = page
            page_tasks = page.get("tasks", [])
            if not isinstance(page_tasks, list):
                raise AdminClientError("change request detail is missing tasks")
            tasks.extend(item for item in page_tasks if isinstance(item, dict))
            total = int(page.get("task_total", len(tasks)))
            if not all_tasks or not page_tasks or len(tasks) >= total:
                detail["tasks"] = tasks
                detail["task_total"] = total
                return detail
            offset += len(page_tasks)

    def list_batches(
        self,
        identity: PullRequestIdentity | None = None,
        *,
        review_task_id: str | None = None,
        statuses: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = identity.params() if identity is not None else {}
        if review_task_id:
            params["review_task_id"] = review_task_id
        items, _total = self.paged_items(
            "/v1/review-issue-batches",
            params=params,
        )
        if statuses:
            allowed = set(statuses)
            return [item for item in items if item.get("status") in allowed]
        return items

    def list_batch_issues(self, batch_id: str) -> list[dict[str, Any]]:
        items, _total = self.paged_items(
            "/v1/review-issues",
            params={"batch_id": batch_id},
        )
        return items

    def show(
        self,
        identity: PullRequestIdentity,
        *,
        task_id: str | None,
        task_statuses: list[str],
        include_result: bool,
        severities: list[str],
        issue_statuses: list[str],
        batch_statuses: list[str],
        category: str | None,
        commit_sha: str | None,
    ) -> dict[str, Any]:
        batches = self.list_batches(identity)
        try:
            detail = self.detail(
                identity,
                task_id=task_id,
                task_statuses=task_statuses,
                include_result=include_result,
            )
        except AdminNotFoundError:
            if task_id or not batches:
                raise
            latest_batch = batches[0]
            detail = {
                "change_request": {
                    **identity.params(),
                    "resource_type": "change_request",
                    "title": None,
                    "url": latest_batch.get("pr_url"),
                    "state": None,
                    "action": None,
                    "source_branch": None,
                    "target_branch": None,
                    "head_sha": latest_batch.get("review_head_sha"),
                    "merged_sha": latest_batch.get("merged_sha"),
                    "last_activity_at": latest_batch.get("updated_at"),
                    "latest_workflow": None,
                    "latest_task": None,
                },
                "workflow_runs": [],
                "tasks": [],
                "task_total": 0,
            }
        issues: list[dict[str, Any]] = []
        issue_summary: dict[str, Any] | None = None
        if batches:
            params: list[tuple[str, Any]] = list(identity.params().items())
            params.extend(("severity", value) for value in severities)
            params.extend(("status", value) for value in issue_statuses)
            params.extend(("batch_status", value) for value in batch_statuses)
            if category:
                params.append(("category", category))
            if commit_sha:
                params.append(("commit_sha", commit_sha))
            offset = 0
            while True:
                page = self.request(
                    "GET",
                    "/v1/review-issues/pull-request",
                    params=[*params, ("offset", offset), ("limit", 200)],
                )
                page_items = page.get("items", [])
                if not isinstance(page_items, list):
                    raise AdminClientError("review issue page is missing items")
                issues.extend(item for item in page_items if isinstance(item, dict))
                issue_summary = page.get("summary")
                total = int(page.get("total", len(issues)))
                if not page_items or len(issues) >= total:
                    break
                offset += len(page_items)
        return {
            **detail,
            "batches": batches,
            "issues": issues,
            "issue_total": len(issues),
            "issue_summary": issue_summary,
        }

    @staticmethod
    def _canonical_issue(issue: dict[str, Any]) -> dict[str, Any]:
        return {
            "severity": issue.get("severity"),
            "category": issue.get("category"),
            "title": issue.get("title"),
            "description": issue.get("description"),
            "file_path": issue.get("file_path"),
            "line_number": issue.get("line_number"),
        }

    @staticmethod
    def _verification_results_match(
        issues: list[dict[str, Any]],
        results: list[VerifyResult],
    ) -> bool:
        by_number = {int(issue["issue_no"]): issue for issue in issues}
        return all(
            item.issue_no in by_number
            and by_number[item.issue_no].get("verification_status") == item.status
            and by_number[item.issue_no].get("verification_note") == item.note
            for item in results
        )

    def collect(
        self,
        identity: PullRequestIdentity,
        *,
        task_id: str | None,
        issues: list[dict[str, Any]],
    ) -> dict[str, Any]:
        detail = self.detail(identity, task_id=task_id)
        tasks = detail.get("tasks", [])
        latest_task = detail.get("change_request", {}).get("latest_task")
        if task_id:
            candidates = [task for task in tasks if task.get("id") == task_id]
        else:
            latest_task_id = (
                latest_task.get("id") if isinstance(latest_task, dict) else None
            )
            candidates = [task for task in tasks if task.get("id") == latest_task_id]
        if len(candidates) != 1:
            raise AdminConflictError(
                "cannot resolve exactly one review task for the pull request"
            )
        task = candidates[0]
        if (
            task.get("status") != "succeeded"
            or task.get("workflow_status") != "succeeded"
            or task.get("is_active") is not True
        ):
            raise AdminConflictError(
                "review task must be active with succeeded task and workflow status"
            )

        batches = self.list_batches(review_task_id=str(task["id"]))
        if len(batches) > 1:
            raise AdminConflictError(
                "review task resolves to multiple collection batches"
            )
        created = False
        if batches:
            batch = batches[0]
        else:
            change_request = detail["change_request"]
            payload = {
                "provider": identity.provider,
                "instance_url": task.get("instance_url"),
                "project_path": identity.project_path,
                "pr_number": identity.pr_number,
                "pr_url": change_request.get("url"),
                "review_workflow_run_id": task.get("workflow_run_id"),
                "review_task_id": task["id"],
                "review_head_sha": change_request.get("head_sha"),
            }
            try:
                batch = self.request("POST", "/v1/review-issue-batches", json=payload)
                created = True
            except AdminConflictError:
                batches = self.list_batches(review_task_id=str(task["id"]))
                if len(batches) != 1:
                    raise
                batch = batches[0]

        batch_id = str(batch["id"])
        if batch.get("status") in {"failed", "cancelled"}:
            raise AdminConflictError(
                f"review task already has a non-recoverable {batch['status']} batch"
            )
        if batch.get("status") == "collecting":
            try:
                self.request(
                    "POST",
                    f"/v1/review-issue-batches/{batch_id}/issues",
                    json={"items": issues},
                )
                idempotent = False
            except AdminConflictError:
                existing = sorted(
                    self.list_batch_issues(batch_id),
                    key=lambda item: int(item["issue_no"]),
                )
                requested_values = [self._canonical_issue(item) for item in issues]
                existing_values = [self._canonical_issue(item) for item in existing]
                if requested_values != existing_values:
                    raise
                idempotent = True
        else:
            existing = sorted(
                self.list_batch_issues(batch_id),
                key=lambda item: int(item["issue_no"]),
            )
            requested_values = [self._canonical_issue(item) for item in issues]
            existing_values = [self._canonical_issue(item) for item in existing]
            if requested_values != existing_values:
                raise AdminConflictError(
                    "review task already has a batch with different issues"
                )
            idempotent = True

        final_batch = self.request("GET", f"/v1/review-issue-batches/{batch_id}")
        final_issues = self.list_batch_issues(batch_id)
        return {
            "ok": True,
            "operation": "collect",
            "idempotent": idempotent,
            "created_batch": created,
            "change_request": detail["change_request"],
            "batch": final_batch,
            "issues": final_issues,
        }

    def add_issues(
        self,
        identity: PullRequestIdentity,
        *,
        issues: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not issues:
            raise AdminInputError("at least one issue is required")
        response = self.request(
            "POST",
            "/v1/review-issues/pull-request",
            json={**identity.params(), "issues": issues},
        )
        return {
            "ok": True,
            "operation": "add_issues",
            **response,
        }

    def verify(
        self,
        identity: PullRequestIdentity,
        *,
        batch_id: str | None,
        merged_sha: str | None,
        results: list[VerifyResult],
    ) -> dict[str, Any]:
        detail = self.detail(identity, include_result=False, all_tasks=False)
        if batch_id:
            batch = self.request("GET", f"/v1/review-issue-batches/{batch_id}")
            if any(batch.get(key) != value for key, value in identity.params().items()):
                raise AdminConflictError(
                    "review issue batch does not belong to the pull request"
                )
        else:
            all_batches = self.list_batches(identity)
            candidates = [
                item
                for item in all_batches
                if item.get("status") in {"waiting_merge", "verifying"}
            ]
            if not candidates and len(all_batches) == 1:
                candidates = all_batches
            if len(candidates) != 1:
                raise AdminConflictError(
                    "expected exactly one waiting_merge or verifying batch; use --batch-id"
                )
            batch = candidates[0]

        batch_id = str(batch["id"])
        issues = self.list_batch_issues(batch_id)
        by_number = {int(issue["issue_no"]): issue for issue in issues}
        missing = [item.issue_no for item in results if item.issue_no not in by_number]
        if missing:
            raise AdminNotFoundError(
                f"issue_no values do not belong to the batch: {missing}"
            )
        if not results and issues:
            raise AdminInputError("results must not be empty for a batch with issues")

        status = str(batch.get("status"))
        if status in {"completed", "failed", "cancelled"}:
            if status != "completed":
                raise AdminConflictError(f"cannot verify a {status} batch")
            if not self._verification_results_match(issues, results):
                raise AdminConflictError(
                    "completed batch has different verification conclusions"
                )
            return {
                "ok": True,
                "operation": "verify",
                "idempotent": True,
                "change_request": detail["change_request"],
                "batch": batch,
                "issues": issues,
            }

        resolved_sha = (
            merged_sha
            or batch.get("merged_sha")
            or detail.get("change_request", {}).get("merged_sha")
        )
        if status == "waiting_merge":
            if not resolved_sha:
                raise AdminInputError("merged SHA is unavailable; provide --merged-sha")
            target_status = "completed" if not issues else "verifying"
            try:
                batch = self.request(
                    "PATCH",
                    f"/v1/review-issue-batches/{batch_id}",
                    json={"status": target_status, "merged_sha": resolved_sha},
                )
            except AdminConflictError:
                current_batch = self.request(
                    "GET", f"/v1/review-issue-batches/{batch_id}"
                )
                current_issues = self.list_batch_issues(batch_id)
                if current_batch.get(
                    "status"
                ) == "completed" and self._verification_results_match(
                    current_issues, results
                ):
                    return {
                        "ok": True,
                        "operation": "verify",
                        "idempotent": True,
                        "change_request": detail["change_request"],
                        "batch": current_batch,
                        "issues": current_issues,
                    }
                raise
            status = str(batch.get("status"))
        elif merged_sha and batch.get("merged_sha") != merged_sha:
            raise AdminConflictError(
                "provided merged SHA differs from the recorded value"
            )

        if results and status == "verifying":
            try:
                self.request(
                    "PATCH",
                    f"/v1/review-issue-batches/{batch_id}/issues",
                    json={
                        "items": [
                            {
                                "id": by_number[item.issue_no]["id"],
                                "status": item.status,
                                "note": item.note,
                            }
                            for item in results
                        ]
                    },
                )
            except AdminConflictError:
                current_batch = self.request(
                    "GET", f"/v1/review-issue-batches/{batch_id}"
                )
                current_issues = self.list_batch_issues(batch_id)
                if current_batch.get(
                    "status"
                ) == "completed" and self._verification_results_match(
                    current_issues, results
                ):
                    return {
                        "ok": True,
                        "operation": "verify",
                        "idempotent": True,
                        "change_request": detail["change_request"],
                        "batch": current_batch,
                        "issues": current_issues,
                    }
                raise

        final_batch = self.request("GET", f"/v1/review-issue-batches/{batch_id}")
        final_issues = self.list_batch_issues(batch_id)
        return {
            "ok": True,
            "operation": "verify",
            "idempotent": False,
            "change_request": detail["change_request"],
            "batch": final_batch,
            "issues": final_issues,
        }


def parse_collect_input(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or "issues" not in payload:
        raise AdminInputError("collect input must be an object containing issues")
    try:
        validated = ReviewIssueBulkCreateRequest(items=payload["issues"])
    except (ValidationError, TypeError) as exc:
        raise AdminInputError(str(exc)) from exc
    return [item.model_dump(mode="json") for item in validated.items]


def parse_add_issues_input(payload: Any) -> list[dict[str, Any]]:
    items = parse_collect_input(payload)
    if not items:
        raise AdminInputError("at least one issue is required")
    return items


def parse_verify_input(payload: Any) -> list[VerifyResult]:
    try:
        validated = VerifyInput.model_validate(payload)
    except ValidationError as exc:
        raise AdminInputError(str(exc)) from exc
    return validated.results


def read_json_input(path: str, stdin_text: str | None = None) -> Any:
    import json

    try:
        text = stdin_text if path == "-" else Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AdminInputError(f"cannot read input: {exc}") from exc
    if text is None:
        raise AdminInputError("stdin input is missing")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AdminInputError(f"invalid JSON input: {exc}") from exc
