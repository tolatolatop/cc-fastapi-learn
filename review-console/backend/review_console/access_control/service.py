import hashlib
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from review_console.config import Settings, get_settings
from review_console.db import SessionLocal
from review_console.models import ConsoleUser, SsoIdentity, utc_now
from review_console.security import hash_password, verify_password


class InactiveSsoUserError(Exception):
    pass


def user_load_options():
    return (
        selectinload(ConsoleUser.grants),
        selectinload(ConsoleUser.sso_identities),
    )


def authenticate_local(
    db: Session, username: str, password: str
) -> ConsoleUser | None:
    user = db.scalar(
        select(ConsoleUser)
        .options(*user_load_options())
        .where(ConsoleUser.username == username.strip().lower())
    )
    if (
        user is None
        or not user.is_active
        or not verify_password(password, user.password_hash)
    ):
        return None
    return user


def _claim(claims: dict[str, Any], path: str) -> Any:
    current: Any = claims
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _username_candidate(claims: dict[str, Any], settings: Settings) -> str:
    raw = _claim(claims, settings.sso_username_claim) or claims.get("email")
    if not isinstance(raw, str) or not raw.strip():
        raw = f"sso-{claims['sub']}"
    if "@" in raw:
        raw = raw.split("@", 1)[0]
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw.strip().lower()).strip("-.")
    return (normalized or "sso-user")[:64]


def _available_username(
    db: Session, candidate: str, *, issuer: str, subject: str
) -> str:
    exists = db.scalar(select(ConsoleUser.id).where(ConsoleUser.username == candidate))
    if exists is None:
        return candidate
    suffix = hashlib.sha256(f"{issuer}\0{subject}".encode()).hexdigest()[:8]
    suffixed = f"{candidate[:55]}-{suffix}"
    counter = 1
    while db.scalar(select(ConsoleUser.id).where(ConsoleUser.username == suffixed)):
        tail = f"-{suffix}-{counter}"
        suffixed = f"{candidate[:64 - len(tail)]}{tail}"
        counter += 1
    return suffixed


def _display_name(
    claims: dict[str, Any], settings: Settings, fallback: str
) -> str:
    raw = _claim(claims, settings.sso_display_name_claim)
    if not isinstance(raw, str) or not raw.strip():
        raw = claims.get("email") or fallback
    return str(raw).strip()[:128]


def _is_admin(claims: dict[str, Any], settings: Settings) -> bool:
    if not settings.sso_admin_group:
        return False
    groups = _claim(claims, settings.sso_groups_claim)
    if isinstance(groups, str):
        return groups == settings.sso_admin_group
    if isinstance(groups, list):
        return settings.sso_admin_group in {str(value) for value in groups}
    return False


def _identity_query(issuer: str, subject: str):
    return (
        select(SsoIdentity)
        .options(
            selectinload(SsoIdentity.user).selectinload(ConsoleUser.grants),
            selectinload(SsoIdentity.user).selectinload(
                ConsoleUser.sso_identities
            ),
        )
        .where(SsoIdentity.issuer == issuer, SsoIdentity.subject == subject)
    )


def find_or_create_sso_user(
    db: Session, claims: dict[str, Any], settings: Settings
) -> ConsoleUser:
    issuer = str(claims["iss"]).rstrip("/")
    subject = str(claims["sub"])
    identity = db.scalar(_identity_query(issuer, subject))
    if identity is not None:
        user = identity.user
        if not user.is_active:
            raise InactiveSsoUserError
        user.display_name = _display_name(claims, settings, user.username)
        if settings.sso_admin_group:
            user.is_admin = _is_admin(claims, settings)
        identity.last_login_at = utc_now()
        user.updated_at = utc_now()
        db.commit()
        return user

    candidate = _username_candidate(claims, settings)
    username = _available_username(db, candidate, issuer=issuer, subject=subject)
    user = ConsoleUser(
        username=username,
        display_name=_display_name(claims, settings, username),
        password_hash="!sso",
        is_admin=_is_admin(claims, settings),
    )
    user.sso_identities.append(SsoIdentity(issuer=issuer, subject=subject))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        identity = db.scalar(_identity_query(issuer, subject))
        if identity is None:
            raise
        if not identity.user.is_active:
            raise InactiveSsoUserError
        return identity.user
    db.refresh(user)
    return user


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
