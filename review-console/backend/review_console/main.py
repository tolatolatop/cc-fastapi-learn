from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from review_console.config import get_settings
from review_console.db import SessionLocal, engine, get_db
from review_console.models import (
    Base,
    ConsoleUser,
    RepositoryGrant,
    RepositoryPermission,
    utc_now,
)
from review_console.security import (
    create_session,
    hash_password,
    read_session,
    verify_password,
)
from review_console.upstream import ReviewApiClient, UpstreamError


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=10, max_length=256)
    is_admin: bool = False


class UserUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    password: str | None = Field(default=None, min_length=10, max_length=256)
    is_admin: bool | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def require_value(self) -> "UserUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        return self


class GrantRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    project_path: str = Field(min_length=1, max_length=255)
    permission: RepositoryPermission

    @field_validator("provider", "project_path")
    @classmethod
    def normalize_scope(cls, value: str) -> str:
        return value.strip().lower().strip("/")


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
        if self.status == "needs_info" and not self.note:
            raise ValueError("需要补充信息时必须填写说明")
        return self


def grant_json(grant: RepositoryGrant) -> dict:
    return {
        "id": grant.id,
        "provider": grant.provider,
        "project_path": grant.project_path,
        "permission": grant.permission,
    }


def user_json(user: ConsoleUser) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
        "is_active": user.is_active,
        "created_at": user.created_at,
        "grants": [grant_json(grant) for grant in user.grants],
    }


def bootstrap_admin() -> None:
    settings = get_settings()
    if not settings.bootstrap_password:
        return
    with SessionLocal() as db:
        exists = db.scalar(select(ConsoleUser).where(ConsoleUser.is_admin.is_(True)))
        if exists is not None:
            return
        db.add(
            ConsoleUser(
                username=settings.bootstrap_username.strip().lower(),
                display_name="审核管理员",
                password_hash=hash_password(settings.bootstrap_password),
                is_admin=True,
            )
        )
        db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    if len(settings.session_secret) < 32:
        raise RuntimeError(
            "REVIEW_CONSOLE_SESSION_SECRET must contain at least 32 characters"
        )
    if not settings.upstream_token:
        raise RuntimeError("REVIEW_CONSOLE_API_TOKEN must be configured")
    Base.metadata.create_all(engine)
    bootstrap_admin()
    yield


app = FastAPI(title="CC Review Console API", lifespan=lifespan)


def current_user(
    review_console_session: Annotated[str | None, Cookie()] = None,
    db: Session = Depends(get_db),
) -> ConsoleUser:
    if not review_console_session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效"
        )
    user_id = read_session(review_console_session, get_settings().session_secret)
    user = db.scalar(
        select(ConsoleUser)
        .options(selectinload(ConsoleUser.grants))
        .where(ConsoleUser.id == user_id, ConsoleUser.is_active.is_(True))
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效"
        )
    return user


def admin_user(user: ConsoleUser = Depends(current_user)) -> ConsoleUser:
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限"
        )
    return user


def permission_for(
    user: ConsoleUser, provider: str, project_path: str
) -> RepositoryPermission | None:
    if user.is_admin:
        return RepositoryPermission.WRITE
    normalized = (provider.lower(), project_path.lower().strip("/"))
    for grant in user.grants:
        if (grant.provider, grant.project_path) == normalized:
            return grant.permission
    return None


def require_scope(
    user: ConsoleUser, provider: str, project_path: str, write: bool = False
) -> None:
    permission = permission_for(user, provider, project_path)
    if permission is None or (write and permission != RepositoryPermission.WRITE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="没有该仓库的操作权限"
        )


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


@app.post("/v1/auth/login")
def login(
    payload: LoginRequest, response: Response, db: Session = Depends(get_db)
) -> dict:
    user = db.scalar(
        select(ConsoleUser)
        .options(selectinload(ConsoleUser.grants))
        .where(ConsoleUser.username == payload.username.strip().lower())
    )
    if (
        user is None
        or not user.is_active
        or not verify_password(payload.password, user.password_hash)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误"
        )
    settings = get_settings()
    response.set_cookie(
        "review_console_session",
        create_session(user.id, settings.session_secret, settings.session_hours),
        max_age=settings.session_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    return {"user": user_json(user)}


@app.post("/v1/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> None:
    response.delete_cookie("review_console_session", path="/", samesite="strict")


@app.get("/v1/auth/me")
def me(user: ConsoleUser = Depends(current_user)) -> dict:
    return user_json(user)


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
    if not user.is_admin:
        params.extend(
            ("repository", f"{grant.provider}/{grant.project_path}")
            for grant in user.grants
        )
        if not user.grants:
            params.append(("repository", "__no_access__/__no_access__"))
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


@app.get("/v1/admin/users")
def list_users(
    _: ConsoleUser = Depends(admin_user), db: Session = Depends(get_db)
) -> dict:
    users = db.scalars(
        select(ConsoleUser)
        .options(selectinload(ConsoleUser.grants))
        .order_by(ConsoleUser.username)
    ).all()
    return {"items": [user_json(user) for user in users]}


@app.post("/v1/admin/users", status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreateRequest,
    _: ConsoleUser = Depends(admin_user),
    db: Session = Depends(get_db),
) -> dict:
    user = ConsoleUser(
        username=payload.username.strip().lower(),
        display_name=payload.display_name.strip(),
        password_hash=hash_password(payload.password),
        is_admin=payload.is_admin,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="用户名已存在"
        ) from exc
    db.refresh(user)
    return user_json(user)


@app.patch("/v1/admin/users/{user_id}")
def update_user(
    user_id: str,
    payload: UserUpdateRequest,
    actor: ConsoleUser = Depends(admin_user),
    db: Session = Depends(get_db),
) -> dict:
    user = db.scalar(
        select(ConsoleUser)
        .options(selectinload(ConsoleUser.grants))
        .where(ConsoleUser.id == user_id)
    )
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    if user.id == actor.id and payload.is_active is False:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="不能停用当前登录用户"
        )
    if user.id == actor.id and payload.is_admin is False:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="不能移除当前用户的管理员角色"
        )
    if payload.display_name is not None:
        user.display_name = payload.display_name.strip()
    if payload.password is not None:
        user.password_hash = hash_password(payload.password)
    if payload.is_admin is not None:
        user.is_admin = payload.is_admin
    if payload.is_active is not None:
        user.is_active = payload.is_active
    user.updated_at = utc_now()
    db.commit()
    return user_json(user)


@app.put("/v1/admin/users/{user_id}/grants")
def put_grant(
    user_id: str,
    payload: GrantRequest,
    _: ConsoleUser = Depends(admin_user),
    db: Session = Depends(get_db),
) -> dict:
    user = db.scalar(
        select(ConsoleUser)
        .options(selectinload(ConsoleUser.grants))
        .where(ConsoleUser.id == user_id)
    )
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    grant = next(
        (
            g
            for g in user.grants
            if g.provider == payload.provider and g.project_path == payload.project_path
        ),
        None,
    )
    if grant is None:
        grant = RepositoryGrant(user_id=user.id, **payload.model_dump())
        db.add(grant)
    else:
        grant.permission = payload.permission
        grant.updated_at = utc_now()
    db.commit()
    db.refresh(grant)
    return grant_json(grant)


@app.delete(
    "/v1/admin/users/{user_id}/grants/{grant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_grant(
    user_id: str,
    grant_id: str,
    _: ConsoleUser = Depends(admin_user),
    db: Session = Depends(get_db),
) -> None:
    grant = db.scalar(
        select(RepositoryGrant).where(
            RepositoryGrant.id == grant_id, RepositoryGrant.user_id == user_id
        )
    )
    if grant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="授权不存在")
    db.delete(grant)
    db.commit()
