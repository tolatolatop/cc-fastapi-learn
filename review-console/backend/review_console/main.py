from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator

from review_console.access_control import (
    admin_user,
    current_user,
    permission_for,
    require_scope,
)
from review_console.access_control.permissions import statistics_repository_params
from review_console.access_control.router import router as access_control_router
from review_console.access_control.service import bootstrap_admin
from review_console.config import get_settings
from review_console.db import engine
from review_console.models import Base, ConsoleUser
from review_console.upstream import ReviewApiClient, UpstreamError

__all__ = ["admin_user", "app", "current_user"]


class StatusUpdateRequest(BaseModel):
    status: Literal["unverified", "accepted", "not_accepted", "needs_info"]
    reason_code: Literal[
        "false_positive",
        "protected_by_control",
        "not_reproducible",
        "duplicate",
        "out_of_scope",
        "intentional_behavior",
        "risk_accepted",
        "other",
    ] | None = None
    note: str | None = Field(default=None, max_length=4000)
    expected_updated_at: datetime

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @model_validator(mode="after")
    def validate_decision_evidence(self) -> "StatusUpdateRequest":
        if self.status == "not_accepted":
            if self.reason_code is None or not self.note:
                raise ValueError("拒绝时必须选择原因分类并填写详细理由")
        elif self.reason_code is not None:
            raise ValueError("只有拒绝意见时可以填写拒绝原因分类")
        return self


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.validate_runtime()
    Base.metadata.create_all(engine)
    bootstrap_admin()
    yield


app = FastAPI(title="CC Review Console API", lifespan=lifespan)
app.include_router(access_control_router)


def upstream_call(call):
    try:
        return call()
    except UpstreamError as exc:
        code = (
            exc.status_code
            if 400 <= exc.status_code < 500
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(status_code=code, detail=exc.detail) from exc


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/repositories")
def repositories(user: ConsoleUser = Depends(current_user)) -> dict:
    payload = upstream_call(ReviewApiClient().repositories)
    items = []
    for item in payload["items"]:
        permission = permission_for(user, item["provider"], item["project_path"])
        if permission is not None:
            items.append({**item, "permission": permission})
    return {"items": items}


@app.get("/v1/issues")
def issues(
    provider: str,
    project_path: str,
    pr_number: str | None = None,
    statuses: list[str] | None = Query(default=None, alias="status"),
    severities: list[str] | None = Query(default=None, alias="severity"),
    query: str | None = Query(default=None, alias="q"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    user: ConsoleUser = Depends(current_user),
) -> dict:
    require_scope(user, provider, project_path)
    params: list[tuple[str, str | int]] = [
        ("provider", provider),
        ("project_path", project_path),
        ("offset", offset),
        ("limit", limit),
    ]
    params.extend(("status", value) for value in statuses or [])
    params.extend(("severity", value) for value in severities or [])
    if pr_number:
        params.append(("pr_number", pr_number))
    if query:
        params.append(("q", query))
    return upstream_call(lambda: ReviewApiClient().issues(params))


@app.get("/v1/pull-requests")
def pull_requests(
    provider: str,
    project_path: str,
    statuses: list[str] | None = Query(default=None, alias="status"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    user: ConsoleUser = Depends(current_user),
) -> dict:
    require_scope(user, provider, project_path)
    params: list[tuple[str, str | int]] = [
        ("provider", provider),
        ("project_path", project_path),
        ("offset", offset),
        ("limit", limit),
    ]
    params.extend(("status", value) for value in statuses or [])
    return upstream_call(lambda: ReviewApiClient().pull_requests(params))


@app.get("/v1/pull-request")
def pull_request(
    provider: str,
    project_path: str,
    pr_number: str,
    user: ConsoleUser = Depends(current_user),
) -> dict:
    require_scope(user, provider, project_path)
    params: list[tuple[str, str | int]] = [
        ("provider", provider),
        ("project_path", project_path),
        ("pr_number", pr_number),
    ]
    return upstream_call(lambda: ReviewApiClient().pull_request(params))


@app.get("/v1/statistics")
def statistics(
    created_from: datetime | None = Query(default=None),
    created_to: datetime | None = Query(default=None),
    user: ConsoleUser = Depends(current_user),
) -> dict:
    if created_from is not None and created_to is not None and created_from > created_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="开始时间不能晚于结束时间",
        )
    params: list[tuple[str, str | int]] = []
    if created_from is not None:
        params.append(("created_from", created_from.isoformat()))
    if created_to is not None:
        params.append(("created_to", created_to.isoformat()))
    params.extend(statistics_repository_params(user))
    return upstream_call(lambda: ReviewApiClient().statistics(params))


@app.get("/v1/issues/{issue_id}/history")
def issue_history(issue_id: str, user: ConsoleUser = Depends(current_user)) -> dict:
    client = ReviewApiClient()
    issue = upstream_call(lambda: client.issue(issue_id))
    require_scope(user, issue["provider"], issue["project_path"])
    return upstream_call(lambda: client.history(issue_id))


@app.put("/v1/issues/{issue_id}/status")
def update_issue_status(
    issue_id: str,
    payload: StatusUpdateRequest,
    user: ConsoleUser = Depends(current_user),
) -> dict:
    client = ReviewApiClient()
    issue = upstream_call(lambda: client.issue(issue_id))
    require_scope(user, issue["provider"], issue["project_path"], write=True)
    return upstream_call(
        lambda: client.update_status(
            issue_id,
            payload.model_dump(mode="json"),
            actor_id=user.id,
            actor_name=user.display_name,
        )
    )
