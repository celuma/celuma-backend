from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlmodel import select, Session
from app.core.db import get_session
from app.api.v1.auth import get_auth_ctx, AuthContext, current_user
from app.core.rbac import has_permission, get_user_roles
from app.models.tenant import Tenant
from app.models.user import AppUser
from app.schemas.tenant import TenantCreate, TenantResponse, TenantDetailResponse
from app.services.managed_tenant_image_service import (
    ManagedTenantImageService,
    InvalidImageError,
    ImageRegistrationError,
)
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
    # Céluma 1.3 Fase 2, Bloque D, Historia D9: exposed here rather than a
    # dedicated endpoint, following the same "reuse the tenant update
    # pattern" recommendation as the other tenant-wide settings above.
    # Reuses the same admin:manage_tenant gate as the rest of this endpoint
    # (see block-c-dependencies.md — this was the open question it left for
    # Bloque D: reuse admin:manage_tenant vs. a new permission).
    reports_v2_enabled: Optional[bool] = None

@router.get("/")
def list_tenants(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
):
    """List all tenants (for admin use)"""
    # By default, restrict to the current tenant only to avoid data leakage.
    tenants = session.exec(select(Tenant).where(Tenant.id == ctx.tenant_id)).all()
    return [
        {"id": str(t.id), "name": t.name, "legal_name": t.legal_name, "reports_v2_enabled": t.reports_v2_enabled}
        for t in tenants
    ]

@router.post("/", response_model=TenantResponse)
def create_tenant(tenant_data: TenantCreate, session: Session = Depends(get_session)):
    """Create a new tenant"""
    tenant = Tenant(name=tenant_data.name, legal_name=tenant_data.legal_name, tax_id=tenant_data.tax_id)
    session.add(tenant)
    session.commit()
    session.refresh(tenant)
    return TenantResponse(id=str(tenant.id), name=tenant.name, legal_name=tenant.legal_name, reports_v2_enabled=tenant.reports_v2_enabled)

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
    return TenantDetailResponse(
        id=str(tenant.id),
        name=tenant.name,
        legal_name=tenant.legal_name,
        tax_id=tenant.tax_id,
        reports_v2_enabled=tenant.reports_v2_enabled,
    )

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
    
    from app.core.rbac import get_user_roles as _roles
    users = [{"id": str(u.id), "email": u.email, "full_name": u.full_name, "roles": _roles(u.id, session)} for u in tenant.users]
    return users


@router.patch("/{tenant_id}", response_model=TenantDetailResponse)
def update_tenant(
    tenant_id: str,
    tenant_data: TenantUpdate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Update tenant details (requires admin:manage_tenant)."""
    if not has_permission(user.id, "admin:manage_tenant", session):
        raise HTTPException(403, "Permission required: admin:manage_tenant")
    
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

    reports_v2_flag_changed = (
        tenant_data.reports_v2_enabled is not None
        and tenant_data.reports_v2_enabled != tenant.reports_v2_enabled
    )
    previous_reports_v2_enabled = tenant.reports_v2_enabled
    if tenant_data.reports_v2_enabled is not None:
        tenant.reports_v2_enabled = tenant_data.reports_v2_enabled

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

    if reports_v2_flag_changed:
        # Céluma 1.3 Fase 2, Bloque D, Historia D9: separate audit line — this
        # flag controls creation of new V2 reports (never reading/rendering
        # existing ones), so a distinct, greppable event is worth having
        # beyond the generic "tenant.updated" line above.
        logger.info(
            f"Tenant {tenant.name} reports_v2_enabled changed: "
            f"{previous_reports_v2_enabled} -> {tenant.reports_v2_enabled}",
            extra={
                "event": "tenant.reports_v2_enabled_changed",
                "tenant_id": str(tenant.id),
                "previous_value": previous_reports_v2_enabled,
                "new_value": tenant.reports_v2_enabled,
                "updated_by": str(user.id),
            },
        )

    return TenantDetailResponse(
        id=str(tenant.id),
        name=tenant.name,
        legal_name=tenant.legal_name,
        tax_id=tenant.tax_id,
        reports_v2_enabled=tenant.reports_v2_enabled,
    )


@router.post("/{tenant_id}/logo")
def upload_tenant_logo(
    tenant_id: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Upload tenant logo (requires admin:manage_tenant)."""
    if not has_permission(user.id, "admin:manage_tenant", session):
        raise HTTPException(403, "Permission required: admin:manage_tenant")
    
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    
    if str(tenant.id) != ctx.tenant_id:
        raise HTTPException(403, "Cannot update different tenant")

    file_bytes = file.file.read()
    try:
        result = ManagedTenantImageService().upload(
            file_bytes=file_bytes,
            declared_content_type=file.content_type or "",
            tenant_id=tenant.id,
            key_prefix=f"tenants/{tenant_id}/logo",
            created_by=user.id,
            session=session,
        )
    except InvalidImageError as exc:
        raise HTTPException(400, exc.message) from None
    except ImageRegistrationError:
        raise HTTPException(500, "Failed to register uploaded logo") from None

    # Update tenant logo_url. Tenant does not carry a logo_storage_id FK
    # (out of scope for this remediation — see managed-logo-upload-contract.md);
    # the StorageObject row created above still exists and is tenant-scoped,
    # it is just not referenced by id from Tenant.
    tenant.logo_url = result.url
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
    """Toggle tenant active status (requires admin:manage_tenant)."""
    if not has_permission(user.id, "admin:manage_tenant", session):
        raise HTTPException(403, "Permission required: admin:manage_tenant")
    
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
