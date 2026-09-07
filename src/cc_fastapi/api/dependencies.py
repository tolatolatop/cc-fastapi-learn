from fastapi import Header, HTTPException, status

from cc_fastapi.core.config import get_settings


def require_token(x_api_token: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not settings.api_token:
        return
    if x_api_token != settings.api_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api token")


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
