from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select, Session
from app.core.db import get_session
from app.api.v1.auth import get_auth_ctx, AuthContext, current_user
from app.core.rbac import has_permission, get_user_roles, FULL_BRANCH_ACCESS_ROLES
from app.models.tenant import Branch, Tenant
from app.models.user import AppUser
from app.models.user_role import UserRoleLink
from app.models.role import Role
from app.schemas.tenant import BranchCreate, BranchResponse, BranchDetailResponse

router = APIRouter(prefix="/branches")


@router.get("/")
def list_branches(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """List all branches (requires lab:read)."""
    if not has_permission(user.id, "lab:read", session):
        raise HTTPException(403, "Permission required: lab:read")

    branches = session.exec(select(Branch).where(Branch.tenant_id == ctx.tenant_id)).all()
    return [{"id": str(b.id), "name": b.name, "code": b.code, "tenant_id": str(b.tenant_id)} for b in branches]


@router.post("/", response_model=BranchResponse)
def create_branch(
    branch_data: BranchCreate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Create a new branch (requires admin:manage_branches)."""
    if not has_permission(user.id, "admin:manage_branches", session):
        raise HTTPException(403, "Permission required: admin:manage_branches")

    # Enforce tenant scoping — branch must be created for the caller's own tenant
    if str(branch_data.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Cannot create branch for a different tenant")

    tenant = session.get(Tenant, branch_data.tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")

    branch = Branch(
        tenant_id=branch_data.tenant_id,
        code=branch_data.code,
        name=branch_data.name,
        timezone=branch_data.timezone,
        address_line1=branch_data.address_line1,
        address_line2=branch_data.address_line2,
        city=branch_data.city,
        state=branch_data.state,
        postal_code=branch_data.postal_code,
        country=branch_data.country,
        is_active=branch_data.is_active if branch_data.is_active is not None else True,
    )
    session.add(branch)
    session.commit()
    session.refresh(branch)
    return BranchResponse(id=str(branch.id), name=branch.name, code=branch.code, tenant_id=str(branch.tenant_id))


@router.get("/{branch_id}", response_model=BranchDetailResponse)
def get_branch(
    branch_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Get branch details (requires lab:read)."""
    if not has_permission(user.id, "lab:read", session):
        raise HTTPException(403, "Permission required: lab:read")

    branch = session.get(Branch, branch_id)
    if not branch:
        raise HTTPException(404, "Branch not found")
    if str(branch.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Branch not found")

    return BranchDetailResponse(
        id=str(branch.id),
        code=branch.code,
        name=branch.name,
        timezone=branch.timezone,
        address_line1=branch.address_line1,
        address_line2=branch.address_line2,
        city=branch.city,
        state=branch.state,
        postal_code=branch.postal_code,
        country=branch.country,
        is_active=branch.is_active,
        tenant_id=str(branch.tenant_id),
    )


@router.get("/{branch_id}/users")
def list_branch_users(
    branch_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """List all users for a branch (requires lab:read)."""
    if not has_permission(user.id, "lab:read", session):
        raise HTTPException(403, "Permission required: lab:read")

    branch = session.get(Branch, branch_id)
    if not branch:
        raise HTTPException(404, "Branch not found")
    if str(branch.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Branch not found")

    users_dict = {}

    # Explicitly assigned users
    for ub in branch.users:
        u = ub.user
        users_dict[u.id] = {
            "id": str(u.id),
            "email": u.email,
            "full_name": u.full_name,
            "roles": get_user_roles(u.id, session),
        }

    # Users with full-branch-access roles (admin / superuser) — implicit access
    full_access_roles = session.exec(
        select(Role).where(Role.code.in_(FULL_BRANCH_ACCESS_ROLES))
    ).all()
    full_access_role_ids = {r.id for r in full_access_roles}

    if full_access_role_ids:
        admin_links = session.exec(
            select(UserRoleLink).where(UserRoleLink.role_id.in_(full_access_role_ids))
        ).all()
        for link in admin_links:
            if link.user_id in users_dict:
                continue
            u = session.get(AppUser, link.user_id)
            if u and str(u.tenant_id) == ctx.tenant_id:
                users_dict[link.user_id] = {
                    "id": str(u.id),
                    "email": u.email,
                    "full_name": u.full_name,
                    "roles": get_user_roles(u.id, session),
                }

    return list(users_dict.values())
