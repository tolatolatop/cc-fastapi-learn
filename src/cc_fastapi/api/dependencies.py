from fastapi import Header, HTTPException, status

from cc_fastapi.core.config import get_settings


def require_token(x_api_token: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not settings.api_token:
        return
    if x_api_token != settings.api_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api token")
