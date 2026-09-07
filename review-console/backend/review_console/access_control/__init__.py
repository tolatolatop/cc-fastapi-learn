"""Authentication, identities, roles, and repository authorization."""

from review_console.access_control.dependencies import admin_user, current_user
from review_console.access_control.permissions import permission_for, require_scope

__all__ = ["admin_user", "current_user", "permission_for", "require_scope"]
