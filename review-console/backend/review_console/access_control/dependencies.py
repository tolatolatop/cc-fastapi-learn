from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from review_console.access_control.oidc import (
    OidcClient,
    OidcUnavailableError,
    OidcValidationError,
)
from review_console.access_control.service import (
    InactiveSsoUserError,
    find_or_create_sso_user,
)
from review_console.config import get_settings
from review_console.db import get_db
from review_console.models import ConsoleUser
from review_console.security import read_session

oauth2_bearer_scheme = HTTPBearer(
    scheme_name="ReviewConsoleOAuth2Bearer",
    auto_error=False,
)


def get_oidc_client() -> OidcClient:
    return OidcClient(get_settings())


def _user_by_id(db: Session, user_id: str | None) -> ConsoleUser | None:
    if user_id is None:
        return None
    return db.scalar(
        select(ConsoleUser)
        .options(
            selectinload(ConsoleUser.grants),
            selectinload(ConsoleUser.sso_identities),
        )
        .where(ConsoleUser.id == user_id, ConsoleUser.is_active.is_(True))
    )


def current_user(
    db: Annotated[Session, Depends(get_db)],
    oidc_client: Annotated[OidcClient, Depends(get_oidc_client)],
    bearer: Annotated[
        HTTPAuthorizationCredentials | None, Security(oauth2_bearer_scheme)
    ] = None,
    review_console_session: Annotated[str | None, Cookie()] = None,
) -> ConsoleUser:
    settings = get_settings()
    if bearer is not None:
        if not settings.oauth_bearer_enabled or bearer.scheme.casefold() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="OAuth 2.0 Bearer 认证未启用",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            settings.validate_runtime()
            claims = oidc_client.validate_access_token(bearer.credentials)
            return find_or_create_sso_user(
                db,
                claims,
                settings,
                synchronize_profile=False,
            )
        except OidcUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OAuth 2.0 身份服务暂不可用",
            ) from exc
        except OidcValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="OAuth 2.0 Access Token 无效",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        except InactiveSsoUserError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="账号已停用",
            ) from exc
        except RuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OAuth 2.0 认证配置无效",
            ) from exc

    user_id = (
        read_session(review_console_session, settings.session_secret)
        if review_console_session
        else None
    )
    user = _user_by_id(db, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录已失效",
            headers={"WWW-Authenticate": "Bearer"}
            if settings.oauth_bearer_enabled
            else None,
        )
    return user


def admin_user(
    user: Annotated[ConsoleUser, Depends(current_user)],
) -> ConsoleUser:
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限"
        )
    return user
