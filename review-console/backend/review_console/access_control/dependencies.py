from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from review_console.config import get_settings
from review_console.db import get_db
from review_console.models import ConsoleUser
from review_console.security import read_session


def current_user(
    db: Annotated[Session, Depends(get_db)],
    review_console_session: Annotated[str | None, Cookie()] = None,
) -> ConsoleUser:
    if not review_console_session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效"
        )
    user_id = read_session(review_console_session, get_settings().session_secret)
    user = db.scalar(
        select(ConsoleUser)
        .options(
            selectinload(ConsoleUser.grants),
            selectinload(ConsoleUser.sso_identities),
        )
        .where(ConsoleUser.id == user_id, ConsoleUser.is_active.is_(True))
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效"
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
