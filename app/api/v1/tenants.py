from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlmodel import select, Session
from uuid import UUID
from app.core.db import get_session
from app.api.v1.auth import get_auth_ctx, AuthContext, current_user
from app.core.rbac import has_permission, get_user_roles, ROLE_REVIEWER
from app.models.tenant import Tenant
from app.models.user import AppUser
from app.schemas.tenant import TenantResponse, TenantDetailResponse, DefaultReviewerRef
from app.services.storage_billing import resolve_current_tenant_logo_storage_object
from app.services.usage_thresholds import record_storage_delta_with_thresholds
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
    # Céluma 1.3 Phase 2, Block D, Story D9: exposed here rather than a
    # dedicated endpoint, following the same "reuse the tenant update
    # pattern" recommendation as the other tenant-wide settings above.
    # Reuses the same admin:manage_tenant gate as the rest of this endpoint
    # (see block-c-dependencies.md — this was the open question it left for
    # Block D: reuse admin:manage_tenant vs. a new permission).
    reports_v2_enabled: Optional[bool] = None
    # Céluma 1.3.1 Block D (CEL-131-06): same "reuse the tenant update
    # pattern" precedent as reports_v2_enabled above. `None` means "not
    # submitted" (leave unchanged) for BOTH of these fields — so clearing the
    # default reviewer needs its own explicit flag, since a plain `null` is
    # indistinguishable from "omitted" once Pydantic parses the body. See
    # docs/celuma-1.3.1/block-d/default-reviewer-contract.md.
    default_reviewer_id: Optional[str] = None
    clear_default_reviewer: bool = False

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

# Céluma 1.3 Phase 5, Block F §1 — E-012: the collection-level
# ``POST /api/v1/tenants/`` route was removed. It was a pre-
# ``/auth/register/unified`` remnant: authenticated but ungated, with no
# frontend caller and no test, and it persisted a Tenant plus a TenantUsage
# row with no branch and no user — an orphan tenant nobody can authenticate
# into. Tenant onboarding is ``POST /api/v1/auth/register/unified``, which
# creates tenant + default branch + admin user atomically.
# See block-e-release-findings.md §4a and block-f-release-findings.md.

def _build_tenant_detail_response(session: Session, tenant: Tenant) -> TenantDetailResponse:
    """Céluma 1.3.1 Block D: shared by `get_tenant` and `update_tenant` so the
    default-reviewer display ref is resolved identically in both places."""
    default_reviewer_ref: Optional[DefaultReviewerRef] = None
    if tenant.default_reviewer_id is not None:
        reviewer_user = session.get(AppUser, tenant.default_reviewer_id)
        if reviewer_user is not None:
            default_reviewer_ref = DefaultReviewerRef(
                id=str(reviewer_user.id),
                full_name=reviewer_user.full_name,
                email=reviewer_user.email,
            )
    return TenantDetailResponse(
        id=str(tenant.id),
        name=tenant.name,
        legal_name=tenant.legal_name,
        tax_id=tenant.tax_id,
        reports_v2_enabled=tenant.reports_v2_enabled,
        default_reviewer_id=(
            str(tenant.default_reviewer_id) if tenant.default_reviewer_id else None
        ),
        default_reviewer=default_reviewer_ref,
    )


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
    return _build_tenant_detail_response(session, tenant)

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

    # Céluma 1.3.1 Block D (CEL-131-06): tenant-level default reviewer.
    # `clear_default_reviewer` wins over `default_reviewer_id` so a caller
    # can never accidentally set-and-clear in the same request; omitting
    # both leaves the current value untouched. Validation mirrors
    # `update_order_reviewers` in laboratory.py exactly (400 for
    # not-found-or-wrong-tenant, 422 for missing the reviewer role) — this is
    # a CONFIGURATION reference, never a grant: it adds no role, no
    # permission and no capability to the chosen user. See
    # docs/celuma-1.3.1/block-d/default-reviewer-contract.md.
    default_reviewer_changed = False
    previous_default_reviewer_id = tenant.default_reviewer_id
    if tenant_data.clear_default_reviewer:
        default_reviewer_changed = tenant.default_reviewer_id is not None
        tenant.default_reviewer_id = None
    elif tenant_data.default_reviewer_id is not None:
        try:
            candidate_id = UUID(tenant_data.default_reviewer_id)
        except ValueError:
            raise HTTPException(400, "default_reviewer_id must be a valid UUID")
        candidate = session.get(AppUser, candidate_id)
        if not candidate or str(candidate.tenant_id) != ctx.tenant_id:
            raise HTTPException(400, f"User {candidate_id} not found or not in tenant")
        if not candidate.is_active:
            raise HTTPException(
                422, "El usuario debe estar activo para ser el revisor predeterminado"
            )
        if ROLE_REVIEWER not in get_user_roles(candidate.id, session):
            raise HTTPException(
                422,
                f"User {candidate_id} does not have the '{ROLE_REVIEWER}' role "
                "and cannot be configured as the default reviewer",
            )
        default_reviewer_changed = candidate.id != previous_default_reviewer_id
        tenant.default_reviewer_id = candidate.id

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
        # Céluma 1.3 Phase 2, Block D, Story D9: separate audit line — this
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

    if default_reviewer_changed:
        # Céluma 1.3.1 Block D: a distinct, greppable line — this is a
        # configuration reference that changes who `submit_report` may fall
        # back to, never a role or permission grant by itself.
        logger.info(
            f"Tenant {tenant.name} default_reviewer_id changed: "
            f"{previous_default_reviewer_id} -> {tenant.default_reviewer_id}",
            extra={
                "event": "tenant.default_reviewer_changed",
                "tenant_id": str(tenant.id),
                "previous_value": str(previous_default_reviewer_id) if previous_default_reviewer_id else None,
                "new_value": str(tenant.default_reviewer_id) if tenant.default_reviewer_id else None,
                "updated_by": str(user.id),
            },
        )

    return _build_tenant_detail_response(session, tenant)


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

    # Céluma 1.3 Phase 4, Block C: resolve the currently-referenced logo
    # BEFORE it is superseded below — only the *current* tenant logo is
    # billable (§12), so a replacement must decrement the outgoing one.
    #
    # Block D: this is now a direct `Tenant.logo_storage_id` FK lookup with
    # an ownership check, not a parse of `logo_url` against the configured
    # CDN prefix. A tenant whose logo was uploaded under a different
    # MEDIA_PUBLIC_BASE_URL is still correctly decremented on replacement.
    previous_logo = resolve_current_tenant_logo_storage_object(session, tenant)
    previous_logo_size_bytes = previous_logo.size_bytes or 0 if previous_logo else 0

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

    # Céluma 1.3 Phase 4, Block D: the FK is the authoritative record of
    # which StorageObject is now the tenant's logo; `logo_url` is the
    # presentation value clients keep reading. Both are written here, in
    # that order of importance — every future "which object is the current
    # logo?" question is answered by `logo_storage_id`, and nothing parses
    # the URL back into a key any more.
    tenant.logo_storage_id = result.storage_object.id
    tenant.logo_url = result.url
    session.add(tenant)
    record_storage_delta_with_thresholds(
        session,
        tenant.id,
        result.size_bytes - previous_logo_size_bytes,
        source="tenant_logo",
        resource_type="tenant_logo",
        actor_id=user.id,
    )
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
