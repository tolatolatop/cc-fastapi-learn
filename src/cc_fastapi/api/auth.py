import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse

from cc_fastapi.auth.oidc import (
    OidcClient,
    OidcUnavailableError,
    OidcValidationError,
    generate_flow_value,
    pkce_challenge,
)
from cc_fastapi.auth.session import (
    create_session,
    create_signed_payload,
    read_session,
    read_signed_payload,
)
from cc_fastapi.core.config import Settings, get_settings

router = APIRouter(prefix="/v1/auth", tags=["auth"])
FLOW_COOKIE = "cc_oidc_flow"
SESSION_COOKIE = "cc_session"
FLOW_MINUTES = 10


def get_oidc_client() -> OidcClient:
    return OidcClient(get_settings())


def _settings_or_503() -> Settings:
    settings = get_settings()
    if not settings.oidc_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="OIDC login is not enabled")
    try:
        settings.validate_oidc_runtime()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OIDC login is not configured correctly",
        ) from exc
    return settings


def _safe_next_url(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return "/"
    return value


@router.get("/config")
def auth_config() -> dict[str, str | bool | None]:
    settings = get_settings()
    return {
        "oidc_enabled": settings.oidc_enabled,
        "login_url": "/v1/auth/login" if settings.oidc_enabled else None,
        "button_label": settings.oidc_button_label,
    }


@router.get("/login")
def login(
    client: Annotated[OidcClient, Depends(get_oidc_client)],
    next_url: Annotated[str | None, Query(alias="next")] = None,
) -> RedirectResponse:
    settings = _settings_or_503()
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
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="OIDC authorization server is unavailable",
        ) from exc
    expires_at = datetime.now(UTC) + timedelta(minutes=FLOW_MINUTES)
    flow = create_signed_payload(
        {
            "kind": "oidc_flow",
            "state": state,
            "nonce": nonce,
            "code_verifier": code_verifier,
            "next": _safe_next_url(next_url),
        },
        settings.oidc_session_secret,
        expires_at,
    )
    response = RedirectResponse(location, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        FLOW_COOKIE,
        flow,
        max_age=FLOW_MINUTES * 60,
        httponly=True,
        secure=settings.oidc_cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/callback")
def callback(
    client: Annotated[OidcClient, Depends(get_oidc_client)],
    flow_cookie: Annotated[str | None, Cookie(alias=FLOW_COOKIE)] = None,
    code: Annotated[str | None, Query()] = None,
    state_value: Annotated[str | None, Query(alias="state")] = None,
    provider_error: Annotated[str | None, Query(alias="error")] = None,
) -> RedirectResponse:
    settings = _settings_or_503()
    if provider_error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="OIDC login was denied")
    flow = read_signed_payload(flow_cookie, settings.oidc_session_secret) if flow_cookie else None
    if (
        flow is None
        or flow.get("kind") != "oidc_flow"
        or not code
        or not state_value
        or not secrets.compare_digest(str(flow.get("state", "")), state_value)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="OIDC login request is invalid or expired",
        )
    try:
        claims = client.authenticate(
            code=code,
            code_verifier=str(flow["code_verifier"]),
            nonce=str(flow["nonce"]),
        )
    except OidcValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="OIDC identity validation failed",
        ) from exc
    except OidcUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="OIDC authorization server is unavailable",
        ) from exc
    response = RedirectResponse(
        _safe_next_url(str(flow.get("next", "/"))),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    response.set_cookie(
        SESSION_COOKIE,
        create_session(claims, settings),
        max_age=settings.oidc_session_hours * 3600,
        httponly=True,
        secure=settings.oidc_cookie_secure,
        samesite="strict",
        path="/",
    )
    response.delete_cookie(FLOW_COOKIE, path="/", samesite="lax")
    return response


@router.get("/me")
def me(
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> dict[str, str]:
    settings = _settings_or_503()
    principal = read_session(session_cookie, settings)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {
        "sub": str(principal["sub"]),
        "username": str(principal["username"]),
        "display_name": str(principal["display_name"]),
        "email": str(principal["email"]),
    }


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", samesite="lax")
