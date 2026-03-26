"""
RBAC management endpoints — Phase 1 (read catalog + assign system roles to users).

Endpoints:
  GET  /rbac/permissions               → full permission catalog (any authenticated user)
  GET  /rbac/roles                     → system roles + their permissions
  GET  /rbac/users/{user_id}/roles     → roles assigned to a user
  PUT  /rbac/users/{user_id}/roles     → replace all roles for a user (admin:manage_users)
"""
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.api.v1.auth import current_user, get_auth_ctx, AuthContext
from app.core.db import get_session
from app.core.rbac import (
    get_user_permissions,
    get_user_roles,
    replace_user_roles,
    ROLE_SUPERUSER,
)
from app.models.permission import Permission
from app.models.role import Role
from app.models.role_permission import RolePermission
from app.models.user import AppUser
from app.models.user_role import UserRoleLink

router = APIRouter(prefix="/rbac")


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class PermissionOut(BaseModel):
    code: str
    domain: str
    display_name: str
    description: Optional[str] = None


class RoleOut(BaseModel):
    code: str
    name: str
    description: Optional[str] = None
    is_system: bool
    is_protected: bool
    permissions: List[str]


class UserRolesOut(BaseModel):
    user_id: str
    roles: List[str]
    permissions: List[str]


class UserRolesIn(BaseModel):
    roles: List[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_manage_users(
    actor: AppUser = Depends(current_user),
    session: Session = Depends(get_session),
) -> AppUser:
    """Inline guard: actor must have admin:manage_users permission."""
    from app.core.rbac import has_permission  # local to avoid any import issue
    if not has_permission(actor.id, "admin:manage_users", session):
        raise HTTPException(403, "Permission required: admin:manage_users")
    return actor


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/permissions", response_model=List[PermissionOut])
def list_permissions(
    session: Session = Depends(get_session),
    _: AppUser = Depends(current_user),
):
    """Return the full permission catalog ordered by domain and code."""
    perms = session.exec(
        select(Permission).order_by(Permission.domain, Permission.code)
    ).all()
    return [
        PermissionOut(
            code=p.code,
            domain=p.domain,
            display_name=p.display_name,
            description=p.description,
        )
        for p in perms
    ]


@router.get("/roles", response_model=List[RoleOut])
def list_roles(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    _: AppUser = Depends(current_user),
):
    """Return system roles (and tenant-scoped custom roles when implemented)."""
    roles = session.exec(
        select(Role).where(
            (Role.tenant_id == None) | (Role.tenant_id == ctx.tenant_id)  # noqa: E711
        ).order_by(Role.is_system.desc(), Role.name)
    ).all()

    result = []
    for role in roles:
        perm_codes = session.exec(
            select(Permission.code)
            .join(RolePermission, Permission.id == RolePermission.permission_id)
            .where(RolePermission.role_id == role.id)
            .order_by(Permission.code)
        ).all()
        result.append(
            RoleOut(
                code=role.code,
                name=role.name,
                description=role.description,
                is_system=role.is_system,
                is_protected=role.is_protected,
                permissions=list(perm_codes),
            )
        )
    return result


@router.get("/users/{user_id}/roles", response_model=UserRolesOut)
def get_user_roles_endpoint(
    user_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    actor: AppUser = Depends(current_user),
):
    """
    Return roles and effective permissions for a user.
    Any authenticated user may query their own profile;
    admin:manage_users is required to query another user.
    """
    if user_id != str(actor.id):
        from app.core.rbac import has_permission
        if not has_permission(actor.id, "admin:manage_users", session):
            raise HTTPException(403, "Permission required: admin:manage_users")

    target = session.get(AppUser, user_id)
    if not target:
        raise HTTPException(404, "User not found")
    if str(target.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "User does not belong to your tenant")

    return UserRolesOut(
        user_id=user_id,
        roles=get_user_roles(target.id, session),
        permissions=sorted(get_user_permissions(target.id, session)),
    )


@router.put("/users/{user_id}/roles", response_model=UserRolesOut)
def set_user_roles(
    user_id: str,
    body: UserRolesIn,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    actor: AppUser = Depends(_require_manage_users),
):
    """
    Replace all roles assigned to a user.

    Rules enforced:
    - Actor must have admin:manage_users.
    - Only superuser may assign or remove the superuser role.
    - Cannot remove all roles from a user (at least one required).
    """
    target = session.get(AppUser, user_id)
    if not target:
        raise HTTPException(404, "User not found")
    if str(target.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "User does not belong to your tenant")

    if not body.roles:
        raise HTTPException(400, "At least one role must be assigned")

    # Only superuser can assign/take away the superuser role
    actor_roles = set(get_user_roles(actor.id, session))
    target_current_roles = set(get_user_roles(target.id, session))
    requested_roles = set(body.roles)

    superuser_being_added = ROLE_SUPERUSER in (requested_roles - target_current_roles)
    superuser_being_removed = ROLE_SUPERUSER in (target_current_roles - requested_roles)

    if (superuser_being_added or superuser_being_removed) and ROLE_SUPERUSER not in actor_roles:
        raise HTTPException(403, "Only a superuser can assign or remove the superuser role")

    try:
        replace_user_roles(target.id, body.roles, session)
        session.commit()
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    return UserRolesOut(
        user_id=user_id,
        roles=get_user_roles(target.id, session),
        permissions=sorted(get_user_permissions(target.id, session)),
    )
