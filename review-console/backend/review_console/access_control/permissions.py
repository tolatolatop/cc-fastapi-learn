from fastapi import HTTPException, status

from review_console.models import ConsoleUser, RepositoryPermission

NO_ACCESS_REPOSITORY = "__no_access__/__no_access__"


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


def statistics_repository_params(user: ConsoleUser) -> list[tuple[str, str]]:
    if user.is_admin:
        return []
    scopes = [
        ("repository", f"{grant.provider}/{grant.project_path}")
        for grant in user.grants
    ]
    return scopes or [("repository", NO_ACCESS_REPOSITORY)]
