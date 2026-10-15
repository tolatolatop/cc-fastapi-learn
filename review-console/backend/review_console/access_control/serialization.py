from review_console.models import ConsoleUser, RepositoryGrant


def grant_json(grant: RepositoryGrant) -> dict:
    return {
        "id": grant.id,
        "provider": grant.provider,
        "project_path": grant.project_path,
        "permission": grant.permission,
    }


def user_json(user: ConsoleUser) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
        "is_active": user.is_active,
        "auth_source": "sso" if user.sso_identities else "local",
        "created_at": user.created_at,
        "grants": [grant_json(grant) for grant in user.grants],
    }
