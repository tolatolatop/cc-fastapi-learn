from typing import Any
from urllib.parse import quote

import httpx

from review_console.config import get_settings


class UpstreamError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


class ReviewApiClient:
    def __init__(self) -> None:
        settings = get_settings()
        self.base_url = settings.upstream_url.rstrip("/")
        self.token = settings.upstream_token

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: list[tuple[str, str | int]] | None = None,
        json: dict[str, Any] | None = None,
        actor_id: str | None = None,
        actor_name: str | None = None,
    ) -> dict[str, Any]:
        headers = {"X-Review-Console-Token": self.token}
        if actor_id is not None:
            headers["X-Review-Actor-Id"] = actor_id
        if actor_name is not None:
            headers["X-Review-Actor-Name"] = quote(actor_name, safe="")
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                params=params,
                json=json,
                timeout=20,
            )
        except httpx.RequestError as exc:
            raise UpstreamError(502, "review data service is unavailable") from exc
        if response.is_error:
            try:
                detail = response.json().get("detail", "review data request failed")
            except ValueError:
                detail = "review data request failed"
            raise UpstreamError(response.status_code, detail)
        return response.json()

    def repositories(self) -> dict[str, Any]:
        return self._request("GET", "/v1/review-console/repositories")

    def issues(self, params: list[tuple[str, str | int]]) -> dict[str, Any]:
        return self._request("GET", "/v1/review-console/issues", params=params)

    def pull_requests(self, params: list[tuple[str, str | int]]) -> dict[str, Any]:
        return self._request("GET", "/v1/review-console/pull-requests", params=params)

    def pull_request(self, params: list[tuple[str, str | int]]) -> dict[str, Any]:
        return self._request("GET", "/v1/review-console/pull-request", params=params)

    def issue(self, issue_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/v1/review-console/issues/{quote(issue_id, safe='')}"
        )

    def update_status(
        self,
        issue_id: str,
        payload: dict[str, Any],
        *,
        actor_id: str,
        actor_name: str,
    ) -> dict[str, Any]:
        return self._request(
            "PUT",
            f"/v1/review-console/issues/{quote(issue_id, safe='')}/status",
            json=payload,
            actor_id=actor_id,
            actor_name=actor_name,
        )

    def history(self, issue_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/v1/review-console/issues/{quote(issue_id, safe='')}/history"
        )

    def statistics(self, params: list[tuple[str, str | int]]) -> dict[str, Any]:
        return self._request("GET", "/v1/review-console/statistics", params=params)
