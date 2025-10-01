from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select, Session
from app.core.db import get_session
from app.api.v1.auth import get_auth_ctx, AuthContext
from app.models.tenant import Tenant
from app.models.user import AppUser
from app.api.v1.auth import current_user
from app.schemas.tenant import TenantCreate, TenantResponse, TenantDetailResponse

router = APIRouter(prefix="/tenants")

@router.get("/")
def list_tenants(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """List all tenants (for admin use)"""
    # By default, restrict to the current tenant only to avoid data leakage.
    tenants = session.exec(select(Tenant).where(Tenant.id == ctx.tenant_id)).all()
    return [{"id": str(t.id), "name": t.name, "legal_name": t.legal_name} for t in tenants]

@router.post("/", response_model=TenantResponse)
def create_tenant(tenant_data: TenantCreate, session: Session = Depends(get_session)):
    """Create a new tenant"""
    tenant = Tenant(name=tenant_data.name, legal_name=tenant_data.legal_name, tax_id=tenant_data.tax_id)
    session.add(tenant)
    session.commit()
    session.refresh(tenant)
    return TenantResponse(id=str(tenant.id), name=tenant.name, legal_name=tenant.legal_name)

@router.get("/{tenant_id}", response_model=TenantDetailResponse)
def get_tenant(
    tenant_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """Get tenant details"""
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    if str(tenant.id) != ctx.tenant_id:
        raise HTTPException(404, "Tenant not found")
    return TenantDetailResponse(id=str(tenant.id), name=tenant.name, legal_name=tenant.legal_name, tax_id=tenant.tax_id)

@router.get("/{tenant_id}/branches")
def list_tenant_branches(
    tenant_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """List all branches for a tenant"""
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    if str(tenant.id) != ctx.tenant_id:
        raise HTTPException(404, "Tenant not found")
    
    branches = [{
        "id": str(b.id),
        "name": b.name,
        "code": b.code
    } for b in tenant.branches]
    return branches

@router.get("/{tenant_id}/users")
def list_tenant_users(
    tenant_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """List all users for a tenant"""
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    if str(tenant.id) != ctx.tenant_id:
        raise HTTPException(404, "Tenant not found")
    
    users = [{"id": str(u.id), "email": u.email, "full_name": u.full_name, "role": u.role} for u in tenant.users]
    return users
