import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from review_console.access_control.dependencies import admin_user, current_user
from review_console.access_control.oidc import (
    OidcClient,
    OidcUnavailableError,
    OidcValidationError,
    generate_flow_value,
    pkce_challenge,
)
from review_console.access_control.schemas import (
    GrantRequest,
    LoginRequest,
    UserCreateRequest,
    UserUpdateRequest,
)
from review_console.access_control.serialization import grant_json, user_json
from review_console.access_control.service import (
    InactiveSsoUserError,
    authenticate_local,
    find_or_create_sso_user,
    user_load_options,
)
from review_console.config import Settings, get_settings
from review_console.db import get_db
from review_console.models import ConsoleUser, RepositoryGrant, utc_now
from review_console.security import (
    create_session,
    create_signed_payload,
    hash_password,
    read_signed_payload,
)

router = APIRouter(prefix="/v1")
SSO_FLOW_COOKIE = "review_console_sso_flow"
SSO_FLOW_MINUTES = 10


def get_oidc_client() -> OidcClient:
    return OidcClient(get_settings())


def _set_session_cookie(response: Response, user_id: str, settings: Settings) -> None:
    response.set_cookie(
        "review_console_session",
        create_session(user_id, settings.session_secret, settings.session_hours),
        max_age=settings.session_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )


def _safe_next_url(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return "/"
    return value


@router.get("/auth/config")
def auth_config() -> dict:
    settings = get_settings()
    return {
        "local_login_enabled": settings.local_login_enabled,
        "sso_enabled": settings.sso_enabled,
        "sso_button_label": settings.sso_button_label,
    }


@router.post("/auth/login")
def login(
    payload: LoginRequest,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    settings = get_settings()
    if not settings.local_login_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="本地账号登录已关闭"
        )
    user = authenticate_local(db, payload.username, payload.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误"
        )
    _set_session_cookie(response, user.id, settings)
    return {"user": user_json(user)}


@router.get("/auth/sso/login")
def sso_login(
    client: Annotated[OidcClient, Depends(get_oidc_client)],
    next_url: Annotated[str | None, Query(alias="next")] = None,
) -> RedirectResponse:
    settings = get_settings()
    if not settings.sso_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="SSO 登录未启用"
        )
    state = generate_flow_value()
    nonce = generate_flow_value()
    code_verifier = generate_flow_value(64)
    try:
        location = client.authorization_url(
            state=state,
            nonce=nonce,
            code_challenge=pkce_challenge(code_verifier),
        )
    except (OidcUnavailableError, OidcValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="SSO 身份服务暂不可用"
        ) from exc
    expires_at = datetime.now(UTC) + timedelta(minutes=SSO_FLOW_MINUTES)
    flow = create_signed_payload(
        {
            "state": state,
            "nonce": nonce,
            "code_verifier": code_verifier,
            "next": _safe_next_url(next_url),
            "exp": int(expires_at.timestamp()),
        },
        settings.session_secret,
    )
    response = RedirectResponse(location, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        SSO_FLOW_COOKIE,
        flow,
        max_age=SSO_FLOW_MINUTES * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/auth/sso/callback")
def sso_callback(
    client: Annotated[OidcClient, Depends(get_oidc_client)],
    db: Annotated[Session, Depends(get_db)],
    sso_flow: Annotated[str | None, Cookie(alias=SSO_FLOW_COOKIE)] = None,
    code: Annotated[str | None, Query()] = None,
    state_value: Annotated[str | None, Query(alias="state")] = None,
    provider_error: Annotated[str | None, Query(alias="error")] = None,
) -> RedirectResponse:
    settings = get_settings()
    if not settings.sso_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="SSO 登录未启用"
        )
    if provider_error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="SSO 登录已取消或被拒绝"
        )
    flow = (
        read_signed_payload(sso_flow, settings.session_secret) if sso_flow else None
    )
    if (
        flow is None
        or not code
        or not state_value
        or not secrets.compare_digest(str(flow.get("state", "")), state_value)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="SSO 登录请求已失效"
        )
    try:
        claims = client.authenticate(
            code=code,
            code_verifier=str(flow["code_verifier"]),
            nonce=str(flow["nonce"]),
        )
    except OidcValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="SSO 身份校验失败"
        ) from exc
    except OidcUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="SSO 身份服务暂不可用"
        ) from exc
    try:
        user = find_or_create_sso_user(db, claims, settings)
    except InactiveSsoUserError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="账号已停用"
        ) from exc
    response = RedirectResponse(
        _safe_next_url(str(flow.get("next", "/"))),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    _set_session_cookie(response, user.id, settings)
    response.delete_cookie(SSO_FLOW_COOKIE, path="/", samesite="lax")
    return response


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> None:
    response.delete_cookie("review_console_session", path="/", samesite="strict")


@router.get("/auth/me")
def me(user: Annotated[ConsoleUser, Depends(current_user)]) -> dict:
    return user_json(user)


@router.get("/admin/users")
def list_users(
    _: Annotated[ConsoleUser, Depends(admin_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    users = db.scalars(
        select(ConsoleUser)
        .options(*user_load_options())
        .order_by(ConsoleUser.username)
    ).all()
    return {"items": [user_json(user) for user in users]}


@router.post("/admin/users", status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreateRequest,
    _: Annotated[ConsoleUser, Depends(admin_user)],
    db: Annotated[Session, Depends(get_db)],
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


@router.patch("/admin/users/{user_id}")
def update_user(
    user_id: str,
    payload: UserUpdateRequest,
    actor: Annotated[ConsoleUser, Depends(admin_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    user = db.scalar(
        select(ConsoleUser)
        .options(*user_load_options())
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
    if payload.password is not None and user.sso_identities:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="SSO 用户不能设置本地密码"
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


@router.put("/admin/users/{user_id}/grants")
def put_grant(
    user_id: str,
    payload: GrantRequest,
    _: Annotated[ConsoleUser, Depends(admin_user)],
    db: Annotated[Session, Depends(get_db)],
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
            item
            for item in user.grants
            if item.provider == payload.provider
            and item.project_path == payload.project_path
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


@router.delete(
    "/admin/users/{user_id}/grants/{grant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_grant(
    user_id: str,
    grant_id: str,
    _: Annotated[ConsoleUser, Depends(admin_user)],
    db: Annotated[Session, Depends(get_db)],
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
