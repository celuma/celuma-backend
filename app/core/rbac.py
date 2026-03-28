"""
RBAC core — pure permission/role resolution functions.

No imports from app.api layer to avoid circular dependencies.
FastAPI dependency factories live in app/api/deps.py.
"""
from typing import Set, List
from uuid import UUID

from sqlmodel import Session, select

from app.models.permission import Permission
from app.models.role import Role
from app.models.role_permission import RolePermission
from app.models.user_role import UserRoleLink
from app.models.user import AppUser


# ---------------------------------------------------------------------------
# System role codes — keep in sync with seed data in the Alembic migration
# ---------------------------------------------------------------------------
ROLE_SUPERUSER = "superuser"
ROLE_ADMIN = "admin"
ROLE_PATHOLOGIST = "pathologist"
ROLE_LAB_TECH = "lab_tech"
ROLE_ASSISTANT = "assistant"
ROLE_BILLING = "billing"
ROLE_VIEWER = "viewer"
ROLE_PHYSICIAN = "physician"

# Roles that have implicit access to all branches of their tenant
FULL_BRANCH_ACCESS_ROLES = {ROLE_SUPERUSER, ROLE_ADMIN}


# ---------------------------------------------------------------------------
# Permission resolution
# ---------------------------------------------------------------------------

def get_user_permissions(user_id: UUID, session: Session) -> Set[str]:
    """Return the set of permission codes effective for a user (union of all assigned roles)."""
    rows = session.exec(
        select(Permission.code)
        .join(RolePermission, Permission.id == RolePermission.permission_id)
        .join(Role, Role.id == RolePermission.role_id)
        .join(UserRoleLink, UserRoleLink.role_id == Role.id)
        .where(UserRoleLink.user_id == user_id)
    ).all()
    return set(rows)


def get_user_roles(user_id: UUID, session: Session) -> List[str]:
    """Return the list of role codes assigned to a user."""
    rows = session.exec(
        select(Role.code)
        .join(UserRoleLink, UserRoleLink.role_id == Role.id)
        .where(UserRoleLink.user_id == user_id)
    ).all()
    return list(rows)


def has_permission(user_id: UUID, permission_code: str, session: Session) -> bool:
    """Check if a user holds a specific permission."""
    return permission_code in get_user_permissions(user_id, session)


def has_any_role(user_id: UUID, role_codes: Set[str], session: Session) -> bool:
    """Return True if the user holds at least one of the given roles."""
    return bool(set(get_user_roles(user_id, session)) & role_codes)


def user_has_full_branch_access(user_id: UUID, session: Session) -> bool:
    """Return True for superuser / admin — these roles implicitly cover all tenant branches."""
    return has_any_role(user_id, FULL_BRANCH_ACCESS_ROLES, session)


# ---------------------------------------------------------------------------
# Tenant-scoped counting helpers
# ---------------------------------------------------------------------------

def count_active_users_with_role(session: Session, tenant_id, role_code: str) -> int:
    """Count active users in a tenant that hold a given role code."""
    users = session.exec(
        select(AppUser).where(
            AppUser.tenant_id == tenant_id,
            AppUser.is_active == True,  # noqa: E712
        )
    ).all()
    return sum(1 for u in users if role_code in get_user_roles(u.id, session))


# ---------------------------------------------------------------------------
# Role management helpers
# ---------------------------------------------------------------------------

def assign_role_by_code(user_id: UUID, role_code: str, session: Session) -> None:
    """Assign a role to a user if not already assigned. Raises ValueError if role not found."""
    role = session.exec(select(Role).where(Role.code == role_code)).first()
    if not role:
        raise ValueError(f"Role '{role_code}' not found in catalog")

    existing = session.exec(
        select(UserRoleLink).where(
            UserRoleLink.user_id == user_id,
            UserRoleLink.role_id == role.id,
        )
    ).first()
    if not existing:
        session.add(UserRoleLink(user_id=user_id, role_id=role.id))


def remove_role_by_code(user_id: UUID, role_code: str, session: Session) -> None:
    """Remove a role from a user (no-op if not assigned)."""
    role = session.exec(select(Role).where(Role.code == role_code)).first()
    if not role:
        return
    link = session.exec(
        select(UserRoleLink).where(
            UserRoleLink.user_id == user_id,
            UserRoleLink.role_id == role.id,
        )
    ).first()
    if link:
        session.delete(link)


def replace_user_roles(user_id: UUID, role_codes: List[str], session: Session) -> None:
    """Replace all roles for a user with the given list. Validates all codes exist first."""
    # Deduplicate while preserving order to avoid duplicate-key errors on insert
    unique_codes = list(dict.fromkeys(role_codes))

    roles = session.exec(select(Role).where(Role.code.in_(unique_codes))).all()
    found_codes = {r.code for r in roles}
    missing = set(unique_codes) - found_codes
    if missing:
        raise ValueError(f"Role(s) not found: {', '.join(sorted(missing))}")

    existing_links = session.exec(
        select(UserRoleLink).where(UserRoleLink.user_id == user_id)
    ).all()
    for link in existing_links:
        session.delete(link)

    # Flush the DELETEs before inserting to avoid unique-constraint violations when
    # the same (user_id, role_id) pair is being re-added and autoflush triggers mid-loop.
    session.flush()

    for role in roles:
        session.add(UserRoleLink(user_id=user_id, role_id=role.id))
