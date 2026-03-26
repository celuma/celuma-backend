"""
Reusable FastAPI dependency factories for authentication + authorization.

Import graph (no cycles):
  app.core.rbac  ← only models + db
  app.api.v1.auth ← models + core (does NOT import deps)
  app.api.deps   ← auth + rbac (safe to import both)
"""
from fastapi import Depends, HTTPException
from sqlmodel import Session

from app.api.v1.auth import current_user
from app.core.db import get_session
from app.core.rbac import has_permission, has_any_role
from app.models.user import AppUser


def require_permission(permission_code: str):
    """
    Dependency factory: returns the current user after asserting they hold the given permission.

    Usage:
        @router.post("/something")
        def endpoint(user: AppUser = Depends(require_permission("reports:sign"))):
            ...
    """
    def dependency(
        user: AppUser = Depends(current_user),
        session: Session = Depends(get_session),
    ) -> AppUser:
        if not has_permission(user.id, permission_code, session):
            raise HTTPException(403, f"Permission required: {permission_code}")
        return user

    # Give FastAPI a meaningful name for error messages / docs
    dependency.__name__ = f"require_{permission_code.replace(':', '_')}"
    return dependency


def require_any_role(*role_codes: str):
    """
    Dependency factory: passes if the user holds at least one of the given role codes.

    Usage:
        @router.get("/something")
        def endpoint(user: AppUser = Depends(require_any_role("admin", "superuser"))):
            ...
    """
    codes = set(role_codes)

    def dependency(
        user: AppUser = Depends(current_user),
        session: Session = Depends(get_session),
    ) -> AppUser:
        if not has_any_role(user.id, codes, session):
            raise HTTPException(403, "Insufficient role for this operation")
        return user

    dependency.__name__ = f"require_roles_{'_or_'.join(sorted(role_codes))}"
    return dependency
