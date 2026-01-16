from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlmodel import select, Session
from app.core.db import get_session
from app.api.v1.auth import get_auth_ctx, AuthContext
from app.models.tenant import Tenant
from app.models.user import AppUser
from app.models.storage import StorageObject
from app.models.enums import UserRole
from app.api.v1.auth import current_user
from app.schemas.tenant import TenantCreate, TenantResponse, TenantDetailResponse
from app.services.s3 import S3Service
from pydantic import BaseModel
from typing import Optional
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tenants")


class TenantUpdate(BaseModel):
    """Schema for updating tenant"""
    name: Optional[str] = None
    legal_name: Optional[str] = None
    tax_id: Optional[str] = None

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


@router.patch("/{tenant_id}", response_model=TenantDetailResponse)
def update_tenant(
    tenant_id: str,
    tenant_data: TenantUpdate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Update tenant details (Admin only)"""
    # Check user role
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Only administrators can update tenant")
    
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    
    if str(tenant.id) != ctx.tenant_id:
        raise HTTPException(403, "Cannot update different tenant")
    
    # Update fields
    if tenant_data.name is not None:
        tenant.name = tenant_data.name
    if tenant_data.legal_name is not None:
        tenant.legal_name = tenant_data.legal_name
    if tenant_data.tax_id is not None:
        tenant.tax_id = tenant_data.tax_id
    
    session.add(tenant)
    session.commit()
    session.refresh(tenant)
    
    logger.info(
        f"Tenant {tenant.name} updated",
        extra={
            "event": "tenant.updated",
            "tenant_id": str(tenant.id),
            "updated_by": str(user.id),
        },
    )
    
    return TenantDetailResponse(
        id=str(tenant.id),
        name=tenant.name,
        legal_name=tenant.legal_name,
        tax_id=tenant.tax_id
    )


@router.post("/{tenant_id}/logo")
def upload_tenant_logo(
    tenant_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Upload tenant logo (Admin only)"""
    # Check user role
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Only administrators can upload logo")
    
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    
    if str(tenant.id) != ctx.tenant_id:
        raise HTTPException(403, "Cannot update different tenant")
    
    # Validate file type
    content_type = (file.content_type or "").lower()
    if not any(img_type in content_type for img_type in ["image/jpeg", "image/jpg", "image/png", "image/webp"]):
        raise HTTPException(400, "Only image files (JPEG, PNG, WEBP) are allowed")
    
    # Upload to S3
    file_bytes = file.file.read()
    if not file_bytes:
        raise HTTPException(400, "Uploaded file is empty")
    
    s3 = S3Service()
    key = f"tenants/{tenant_id}/logo.{file.filename.split('.')[-1]}"
    info = s3.upload_bytes(file_bytes, key=key, content_type=content_type)
    
    # Update tenant logo_url
    tenant.logo_url = s3.object_public_url(key)
    session.add(tenant)
    session.commit()
    
    logger.info(
        f"Logo uploaded for tenant {tenant.name}",
        extra={
            "event": "tenant.logo_uploaded",
            "tenant_id": str(tenant.id),
            "uploaded_by": str(user.id),
        },
    )
    
    return {
        "message": "Logo uploaded successfully",
        "logo_url": tenant.logo_url
    }


@router.post("/{tenant_id}/toggle")
def toggle_tenant_active(
    tenant_id: str,
    session: Session = Depends(get_session),
    user: AppUser = Depends(current_user),
):
    """Toggle tenant active status (Super Admin only - normally restricted)"""
    # For now, only allow if user is admin of the tenant
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Only administrators can toggle tenant status")
    
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    
    # Prevent deactivating own tenant
    if str(tenant.id) == str(user.tenant_id):
        raise HTTPException(400, "Cannot deactivate your own tenant")
    
    tenant.is_active = not tenant.is_active
    session.add(tenant)
    session.commit()
    
    logger.info(
        f"Tenant {tenant.name} status toggled",
        extra={
            "event": "tenant.toggled",
            "tenant_id": str(tenant.id),
            "toggled_by": str(user.id),
            "new_status": tenant.is_active,
        },
    )
    
    return {
        "message": f"Tenant {'activated' if tenant.is_active else 'deactivated'}",
        "is_active": tenant.is_active
    }
