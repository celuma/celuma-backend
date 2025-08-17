from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select, Session
from app.core.db import get_session
from app.models.tenant import Tenant
from app.models.user import AppUser
from app.api.v1.auth import current_user

router = APIRouter(prefix="/tenants")

@router.get("/")
def list_tenants(session: Session = Depends(get_session)):
    """List all tenants (for admin use)"""
    tenants = session.exec(select(Tenant)).all()
    return [{"id": str(t.id), "name": t.name, "legal_name": t.legal_name} for t in tenants]

@router.post("/")
def create_tenant(name: str, legal_name: str = None, tax_id: str = None, session: Session = Depends(get_session)):
    """Create a new tenant"""
    tenant = Tenant(name=name, legal_name=legal_name, tax_id=tax_id)
    session.add(tenant)
    session.commit()
    session.refresh(tenant)
    return {"id": str(tenant.id), "name": tenant.name, "legal_name": tenant.legal_name}

@router.get("/{tenant_id}")
def get_tenant(tenant_id: str, session: Session = Depends(get_session)):
    """Get tenant details"""
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    return {"id": str(tenant.id), "name": tenant.name, "legal_name": tenant.legal_name}

@router.get("/{tenant_id}/branches")
def list_tenant_branches(tenant_id: str, session: Session = Depends(get_session)):
    """List all branches for a tenant"""
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    
    branches = [{"id": str(b.id), "name": b.name, "code": b.code, "city": b.city} for b in tenant.branches]
    return branches

@router.get("/{tenant_id}/users")
def list_tenant_users(tenant_id: str, session: Session = Depends(get_session)):
    """List all users for a tenant"""
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    
    users = [{"id": str(u.id), "email": u.email, "full_name": u.full_name, "role": u.role} for u in tenant.users]
    return users
