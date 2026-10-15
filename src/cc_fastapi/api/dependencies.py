import secrets
from typing import Annotated, Any

from fastapi import Cookie, Header, HTTPException, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from cc_fastapi.auth.oidc import OidcClient, OidcUnavailableError, OidcValidationError
from cc_fastapi.auth.session import read_session
from cc_fastapi.core.config import get_settings

api_token_scheme = APIKeyHeader(name="X-API-Token", scheme_name="ApiToken", auto_error=False)
oauth2_bearer_scheme = HTTPBearer(scheme_name="OAuth2Bearer", auto_error=False)


def require_token(
    x_api_token: Annotated[str | None, Security(api_token_scheme)] = None,
    bearer: Annotated[
        HTTPAuthorizationCredentials | None, Security(oauth2_bearer_scheme)
    ] = None,
    session_cookie: Annotated[str | None, Cookie(alias="cc_session")] = None,
) -> dict[str, Any] | None:
    """Accept a legacy API token, OAuth 2.0 bearer JWT, or OIDC browser session."""
    settings = get_settings()
    expected_api_token = settings.api_token.strip()
    if expected_api_token and x_api_token and secrets.compare_digest(x_api_token, expected_api_token):
        return {"auth_type": "api_token"}

    if settings.oidc_enabled:
        try:
            settings.validate_oidc_runtime()
        except RuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OIDC authentication is not configured correctly",
            ) from exc
        principal = read_session(session_cookie, settings)
        if principal is not None:
            return principal
        if bearer is not None and bearer.scheme.casefold() == "bearer" and bearer.credentials.strip():
            try:
                return OidcClient(settings).validate_access_token(bearer.credentials.strip())
            except OidcUnavailableError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="OIDC authorization server is unavailable",
                ) from exc
            except OidcValidationError:
                pass

    if not expected_api_token and not settings.oidc_enabled:
        return None
    detail = "invalid api token" if not settings.oidc_enabled else "authentication required"
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"} if settings.oidc_enabled else None,
    )


def require_review_console_token(
    x_review_console_token: str | None = Header(default=None),
) -> None:
    """Authenticate the separately deployed review console backend."""
    settings = get_settings()
    expected = settings.review_console_api_token.strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="review console integration is not configured",
        )
    if x_review_console_token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid review console token",
        )
