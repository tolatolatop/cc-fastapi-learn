from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from cc_fastapi.core.config import Settings

SESSION_ISSUER = "cc-fastapi"
SESSION_AUDIENCE = "cc-fastapi-console"


def create_signed_payload(payload: dict[str, Any], secret: str, expires_at: datetime) -> str:
    return jwt.encode(
        {
            **payload,
            "iat": datetime.now(UTC),
            "exp": expires_at,
            "iss": SESSION_ISSUER,
            "aud": SESSION_AUDIENCE,
        },
        secret,
        algorithm="HS256",
    )


def read_signed_payload(token: str, secret: str) -> dict[str, Any] | None:
    try:
        return jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            issuer=SESSION_ISSUER,
            audience=SESSION_AUDIENCE,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except (jwt.PyJWTError, TypeError, ValueError):
        return None


def create_session(claims: dict[str, Any], settings: Settings) -> str:
    expires_at = datetime.now(UTC) + timedelta(hours=settings.oidc_session_hours)
    return create_signed_payload(
        {
            "kind": "session",
            "sub": str(claims["sub"]),
            "username": str(
                claims.get(settings.oidc_username_claim)
                or claims.get("email")
                or claims["sub"]
            ),
            "display_name": str(
                claims.get(settings.oidc_display_name_claim)
                or claims.get(settings.oidc_username_claim)
                or claims["sub"]
            ),
            "email": str(claims.get(settings.oidc_email_claim) or ""),
        },
        settings.oidc_session_secret,
        expires_at,
    )


def read_session(token: str | None, settings: Settings) -> dict[str, Any] | None:
    if not token or not settings.oidc_enabled or not settings.oidc_session_secret:
        return None
    payload = read_signed_payload(token, settings.oidc_session_secret)
    if payload is None or payload.get("kind") != "session" or not payload.get("sub"):
        return None
    return payload
