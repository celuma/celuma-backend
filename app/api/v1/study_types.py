from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlmodel import select, Session
from app.core.db import get_session
from app.api.v1.auth import get_auth_ctx, AuthContext, current_user
from app.core.rbac import has_permission
from app.models.study_type import StudyType
from app.models.report import ReportTemplate
from app.models.report_letterhead import ReportLetterhead
from app.models.report_letterhead_version import (
    ReportLetterheadVersion,
    ReportLetterheadVersionStatus,
)
from app.models.tenant import Tenant
from app.models.user import AppUser
from app.schemas.study_type import (
    CatalogTypeRemovalImpact,
    CatalogTypeRemoveRequest,
    StudyTypeRemovalResponse,
    StudyTypeCreate,
    StudyTypeUpdate,
    StudyTypeResponse,
    StudyTypeDetailResponse,
    StudyTypesListResponse,
    StudyTypeReportDefaultsResponse,
    TemplateRef,
)
from app.services import catalog_removal, catalog_transfer, report_defaults
from app.services.sample_types import record_audit, snapshot
import logging
from uuid import UUID

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/study-types")


def _code_taken_message(code: str, clash: StudyType) -> str:
    if clash.removed_at is not None:
        return (f"Study type with code '{code}' already exists: the code belongs to a type removed from the "
                "catalog and stays reserved because orders reference it")
    return f"Study type with code '{code}' already exists"


@router.get("/", response_model=StudyTypesListResponse)
def list_study_types(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
    active_only: bool = True,
    include_removed: bool = False,
):
    """List all study types for the tenant (requires lab:read).

    Céluma 1.5 (#94): a removed type is not part of the catalog and is left out unless
    `include_removed=true`; it is never active, so `active_only=true` never returns it."""
    if not has_permission(user.id, "lab:read", session):
        raise HTTPException(403, "Permission required: lab:read")
    query = select(StudyType).where(StudyType.tenant_id == ctx.tenant_id)
    if not include_removed:
        query = query.where(catalog_removal.not_removed(StudyType))
    
    if active_only:
        query = query.where(StudyType.is_active == True)
    
    study_types = session.exec(query).all()
    
    results = []
    for t in study_types:
        # Get template reference if set
        template_ref = None
        if t.default_report_template_id:
            template = session.get(ReportTemplate, t.default_report_template_id)
            if template:
                template_ref = TemplateRef(id=str(template.id), name=template.name)
        
        results.append(
            StudyTypeResponse(
                id=str(t.id),
                tenant_id=str(t.tenant_id),
                code=t.code,
                name=t.name,
                description=t.description,
                is_active=t.is_active,
                created_at=t.created_at,
                default_report_template_id=str(t.default_report_template_id) if t.default_report_template_id else None,
                default_template=template_ref,
                is_removed=t.removed_at is not None,
                removed_at=t.removed_at,
                code_released=t.code_released_at is not None,
            )
        )
    
    return StudyTypesListResponse(study_types=results)


@router.get("/export")
def export_study_types(
    include_inactive: bool = True,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Download the whole study-type catalog as versioned JSON (requires
    admin:manage_catalog). Read-only; see catalog-transfer.md (issue #17)."""
    if not has_permission(user.id, "admin:manage_catalog", session):
        raise HTTPException(403, "Permission required: admin:manage_catalog")
    document = catalog_transfer.build_export(session, ctx.tenant_id, "study_types", include_inactive=include_inactive)
    record_audit(
        session, tenant_id=ctx.tenant_id, user_id=user.id, action="CATALOG.EXPORTED",
        entity_type="catalog_export", entity_id=ctx.tenant_id,
        description="Study-type catalog exported",
        metadata={"catalog_kind": "study_types", "item_count": document["item_count"],
                  "include_inactive": include_inactive},
    )
    session.commit()
    return JSONResponse(
        document,
        headers={"Content-Disposition": f'attachment; filename="{catalog_transfer.export_filename("study_types")}"'},
    )


@router.get("/{study_type_id}", response_model=StudyTypeDetailResponse)
def get_study_type(
    study_type_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Get a specific study type by ID (requires lab:read)."""
    if not has_permission(user.id, "lab:read", session):
        raise HTTPException(403, "Permission required: lab:read")
    study_type = session.get(StudyType, study_type_id)
    if not study_type:
        raise HTTPException(404, "Study type not found")
    
    if str(study_type.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Study type not found")
    
    # Get template reference if set
    template_ref = None
    if study_type.default_report_template_id:
        template = session.get(ReportTemplate, study_type.default_report_template_id)
        if template:
            template_ref = TemplateRef(id=str(template.id), name=template.name)
    
    return StudyTypeDetailResponse(
        id=str(study_type.id),
        tenant_id=str(study_type.tenant_id),
        code=study_type.code,
        name=study_type.name,
        description=study_type.description,
        is_active=study_type.is_active,
        created_at=study_type.created_at,
        default_report_template_id=str(study_type.default_report_template_id) if study_type.default_report_template_id else None,
        default_template=template_ref,
        is_removed=study_type.removed_at is not None,
        removed_at=study_type.removed_at,
        code_released=study_type.code_released_at is not None,
    )


@router.post("/", response_model=StudyTypeResponse)
def create_study_type(
    study_type_data: StudyTypeCreate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Create a new study type (requires admin:manage_catalog)."""
    if not has_permission(user.id, "admin:manage_catalog", session):
        raise HTTPException(403, "Permission required: admin:manage_catalog")
    # Check if code already exists for this tenant. #94: a code released by an unused removed type is free;
    # the lock serialises this check with other creates/renames/removals of this catalog (no DB constraint).
    catalog_removal.lock_catalog_codes(session, "study_type", ctx.tenant_id)
    existing = session.exec(
        select(StudyType).where(
            StudyType.tenant_id == ctx.tenant_id,
            StudyType.code == study_type_data.code.upper(),
            catalog_removal.reserves_code(StudyType),
        )
    ).first()
    
    if existing:
        raise HTTPException(400, _code_taken_message(study_type_data.code, existing))
    
    # Validate default_report_template_id if provided
    if study_type_data.default_report_template_id:
        template = session.get(ReportTemplate, study_type_data.default_report_template_id)
        if not template:
            raise HTTPException(404, "Report template not found")
        if str(template.tenant_id) != ctx.tenant_id:
            raise HTTPException(403, "Report template does not belong to your tenant")
    
    study_type = StudyType(
        tenant_id=ctx.tenant_id,
        code=study_type_data.code.upper(),
        name=study_type_data.name,
        description=study_type_data.description,
        is_active=study_type_data.is_active if study_type_data.is_active is not None else True,
        default_report_template_id=study_type_data.default_report_template_id,
    )
    
    session.add(study_type)
    session.commit()
    session.refresh(study_type)
    
    # Get template reference if set
    template_ref = None
    if study_type.default_report_template_id:
        template = session.get(ReportTemplate, study_type.default_report_template_id)
        if template:
            template_ref = TemplateRef(id=str(template.id), name=template.name)
    
    logger.info(
        f"Study type '{study_type.code}' created",
        extra={
            "event": "study_type.created",
            "study_type_id": str(study_type.id),
            "user_id": str(user.id),
        },
    )
    
    return StudyTypeResponse(
        id=str(study_type.id),
        tenant_id=str(study_type.tenant_id),
        code=study_type.code,
        name=study_type.name,
        description=study_type.description,
        is_active=study_type.is_active,
        created_at=study_type.created_at,
        default_report_template_id=str(study_type.default_report_template_id) if study_type.default_report_template_id else None,
        default_template=template_ref,
        is_removed=study_type.removed_at is not None,
        removed_at=study_type.removed_at,
        code_released=study_type.code_released_at is not None,
    )


@router.put("/{study_type_id}", response_model=StudyTypeResponse)
def update_study_type(
    study_type_id: str,
    study_type_data: StudyTypeUpdate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Update an existing study type (requires admin:manage_catalog)."""
    if not has_permission(user.id, "admin:manage_catalog", session):
        raise HTTPException(403, "Permission required: admin:manage_catalog")
    study_type = session.get(StudyType, study_type_id)
    if not study_type:
        raise HTTPException(404, "Study type not found")
    
    if str(study_type.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Study type does not belong to your tenant")
    # Céluma 1.5 (#94): a removed type is frozen; an ordinary edit can neither change nor reactivate it.
    catalog_removal.ensure_not_removed(study_type, "study_type", purpose="or edited")
    
    # If updating code, check for duplicates
    if study_type_data.code is not None:
        catalog_removal.lock_catalog_codes(session, "study_type", ctx.tenant_id)
        existing = session.exec(
            select(StudyType).where(
                StudyType.tenant_id == ctx.tenant_id,
                StudyType.code == study_type_data.code.upper(),
                StudyType.id != study_type.id,
                catalog_removal.reserves_code(StudyType),
            )
        ).first()
        
        if existing:
            raise HTTPException(400, _code_taken_message(study_type_data.code, existing))
        
        study_type.code = study_type_data.code.upper()
    
    # Validate default_report_template_id if provided. Céluma 1.4.1: the study type's template is an optional
    # default, so it must be clearable: omitted = unchanged; an explicit null or blank clears it (before, null was
    # ignored and a blank reached the UUID column and failed). Same semantics as the sample-type link.
    if "default_report_template_id" in study_type_data.model_fields_set:
        if study_type_data.default_report_template_id:
            template = session.get(ReportTemplate, study_type_data.default_report_template_id)
            if not template:
                raise HTTPException(404, "Report template not found")
            if str(template.tenant_id) != ctx.tenant_id:
                raise HTTPException(403, "Report template does not belong to your tenant")
        study_type.default_report_template_id = study_type_data.default_report_template_id or None
    
    # Update fields if provided
    if study_type_data.name is not None:
        study_type.name = study_type_data.name
    if study_type_data.description is not None:
        study_type.description = study_type_data.description
    if study_type_data.is_active is not None:
        study_type.is_active = study_type_data.is_active
    
    session.add(study_type)
    session.commit()
    session.refresh(study_type)
    
    # Get template reference if set
    template_ref = None
    if study_type.default_report_template_id:
        template = session.get(ReportTemplate, study_type.default_report_template_id)
        if template:
            template_ref = TemplateRef(id=str(template.id), name=template.name)
    
    logger.info(
        f"Study type '{study_type.code}' updated",
        extra={
            "event": "study_type.updated",
            "study_type_id": str(study_type.id),
            "user_id": str(user.id),
        },
    )
    
    return StudyTypeResponse(
        id=str(study_type.id),
        tenant_id=str(study_type.tenant_id),
        code=study_type.code,
        name=study_type.name,
        description=study_type.description,
        is_active=study_type.is_active,
        created_at=study_type.created_at,
        default_report_template_id=str(study_type.default_report_template_id) if study_type.default_report_template_id else None,
        default_template=template_ref,
        is_removed=study_type.removed_at is not None,
        removed_at=study_type.removed_at,
        code_released=study_type.code_released_at is not None,
    )


@router.delete("/{study_type_id}")
def delete_study_type(
    study_type_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
    hard_delete: bool = False,
):
    """Delete a study type (requires admin:manage_catalog)."""
    if not has_permission(user.id, "admin:manage_catalog", session):
        raise HTTPException(403, "Permission required: admin:manage_catalog")
    study_type = session.get(StudyType, study_type_id)
    if not study_type:
        raise HTTPException(404, "Study type not found")
    
    if str(study_type.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Study type does not belong to your tenant")
    # Céluma 1.5 (#94): a removed type is retained on purpose (its code stays reserved); neither deactivating
    # it again nor erasing it is an ordinary catalog action.
    catalog_removal.ensure_not_removed(study_type, "study_type", purpose="or deleted")
    
    if hard_delete:
        # Permanently delete the study type
        # Céluma 1.5 (#64): a type's own numbering goes with it (the row has no FK; counters and ledger stay).
        session.execute(
            text("DELETE FROM numbering_type_config WHERE tenant_id = :t AND entity_kind = :k AND type_id = :i"),
            {"t": str(study_type.tenant_id), "k": "order", "i": str(study_type.id)},
        )
        session.delete(study_type)
        session.commit()
        
        logger.info(
            f"Study type '{study_type.code}' permanently deleted",
            extra={
                "event": "study_type.hard_deleted",
                "study_type_id": study_type_id,
                "user_id": str(user.id),
            },
        )
        
        return {"message": "Study type permanently deleted", "id": study_type_id}
    else:
        # Soft delete - just mark as inactive
        study_type.is_active = False
        session.add(study_type)
        session.commit()
        
        logger.info(
            f"Study type '{study_type.code}' soft deleted (deactivated)",
            extra={
                "event": "study_type.soft_deleted",
                "study_type_id": str(study_type.id),
                "user_id": str(user.id),
            },
        )
        
        return {"message": "Study type deactivated", "id": str(study_type.id)}


@router.post("/{study_type_id}/remove", response_model=StudyTypeRemovalResponse)
def remove_study_type(
    study_type_id: str,
    body: CatalogTypeRemoveRequest | None = None,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Remove a study type from the catalog (requires admin:manage_catalog; Céluma 1.5, #94).

    A retained tombstone, distinct from deactivation: the row, id and code stay; orders, prices, invoice
    items, numbering configuration and counters keep their references; the type disappears from the
    catalog and every selector and is refused for new orders, prices, copies, edits and numbering changes.
    Referenced or not, the type is kept (no data is erased). Its code is RELEASED for another study type
    only when no order, withdrawn ones included, references it (owner decision 2026-11-25). Unknown, malformed and other tenants' ids are
    404 (no existence leak). Repeating the request answers the same removal and writes nothing. Unlike the
    pre-1.5 study-type mutations, which log only, removal writes an `audit_log` row.
    """
    if not has_permission(user.id, "admin:manage_catalog", session):
        raise HTTPException(403, "Permission required: admin:manage_catalog")
    try:
        study_type = session.get(StudyType, UUID(str(study_type_id)))
    except ValueError:
        study_type = None
    if study_type is None or str(study_type.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Study type not found")
    reason = catalog_removal.clean_reason(body.reason if body else None)
    # Code lock first, then the row: serialises with creates/renames, concurrent removals and order
    # registrations that hold the type FOR SHARE, so the count below is final.
    catalog_removal.lock_catalog_codes(session, "study_type", ctx.tenant_id)
    session.refresh(study_type, with_for_update=True)
    refs = catalog_removal.references(session, "study_type", study_type.id)
    release = not catalog_removal.has_history("study_type", refs)
    already = study_type.removed_at is not None
    if not already:
        before = snapshot(study_type)
        catalog_removal.mark_removed(study_type, user_id=user.id, reason=reason, release_code=release)
        session.add(study_type)
        record_audit(
            session, tenant_id=ctx.tenant_id, user_id=user.id, action="STUDY_TYPE.REMOVED",
            entity_type="study_type", entity_id=study_type.id, old_values=before, new_values=snapshot(study_type),
            description=f"Study type '{study_type.code}' removed from the catalog"
            + ("; code released" if release else "; code kept reserved by its orders"),
            metadata={"references": refs, "was_active": before["is_active"], "code_released": release},
        )
        session.commit()
        session.refresh(study_type)
        logger.info(
            f"Study type '{study_type.code}' removed",
            extra={"event": "study_type.removed", "study_type_id": str(study_type.id), "user_id": str(user.id)},
        )
    template_ref = None
    if study_type.default_report_template_id:
        template = session.get(ReportTemplate, study_type.default_report_template_id)
        if template:
            template_ref = TemplateRef(id=str(template.id), name=template.name)
    return StudyTypeRemovalResponse(
        id=str(study_type.id),
        tenant_id=str(study_type.tenant_id),
        code=study_type.code,
        name=study_type.name,
        description=study_type.description,
        is_active=study_type.is_active,
        created_at=study_type.created_at,
        default_report_template_id=str(study_type.default_report_template_id) if study_type.default_report_template_id else None,
        default_template=template_ref,
        is_removed=True,
        removed_at=study_type.removed_at,
        code_released=study_type.code_released_at is not None,
        already_removed=already,
        references=refs,
    )


@router.get("/{study_type_id}/report-defaults", response_model=StudyTypeReportDefaultsResponse)
def get_study_type_report_defaults(
    study_type_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Resolve everything the report editor needs to bootstrap a brand-new
    V2 report in one round trip (requires lab:read).

    Post-Phase-2 remediation, R7; deterministic resolution and presentation
    included in the third remediation. Uses the same
    `resolve_effective_letterhead_version` as `create_report`, so what the
    editor shows and what ends up embedded in the report cannot diverge.
    Never raises for an unconfigured tenant: instead returns
    `v2_blocked_reason` with the exact reason, so the UI can show an
    actionable blocked state instead of falling back to Legacy.
    """
    if not has_permission(user.id, "lab:read", session):
        raise HTTPException(403, "Permission required: lab:read")
    study_type = session.get(StudyType, study_type_id)
    if not study_type or str(study_type.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Study type not found")

    template_id = study_type.default_report_template_id
    owning_template = session.get(ReportTemplate, template_id) if template_id is not None else None
    # Shared with the order bootstrap (Céluma 1.4.1): the same `resolve_effective_letterhead_version` as
    # `create_report`, so the editor's preview and the frozen report cannot diverge.
    letterhead = report_defaults.resolve_letterhead_defaults(session, ctx.tenant_id, owning_template)

    blocked_reason = None
    blocked_detail = None
    if letterhead.misconfigured:
        blocked_reason = "LETTERHEAD_MISCONFIGURED"
        blocked_detail = letterhead.misconfigured_detail
    elif template_id is None:
        blocked_reason = "NO_TEMPLATE"
    elif not letterhead.resolved:
        blocked_reason = "NO_LETTERHEAD"
    # Céluma 1.3.1 Block C (CEL-131-05): `NO_ACTIVE_TEMPLATE_VERSION` used to
    # sit between these two and was the production regression. A V2 report
    # needs a clinical template and a resolvable letterhead, and nothing else
    # — its structure is frozen into its own `rendering_snapshot` at creation
    # (`create_report`, `template_id` branch), so whether an administrative
    # `ReportTemplateVersion` happens to be ACTIVE is irrelevant to authoring.
    # A laboratory that saved its template before configuring its letterhead
    # had no version row, could never get one without re-saving the template,
    # and was blocked out of Reports V2 entirely.
    #
    # `active_template_version_id` below is still returned, as diagnostic and
    # provenance information for the template-administration screens. It is
    # NOT a precondition and the report editor must not treat it as one.

    return StudyTypeReportDefaultsResponse(
        template_id=str(template_id) if template_id else None,
        active_template_version_id=report_defaults.active_template_version_id(session, owning_template),
        **letterhead.response_fields(),
        v2_blocked_reason=blocked_reason,
        v2_blocked_detail=blocked_detail,
    )


@router.get("/{study_type_id}/removal-impact", response_model=CatalogTypeRemovalImpact)
def study_type_removal_impact(
    study_type_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """What removing this study type would do now (requires admin:manage_catalog; #94). Reads only;
    the removal itself decides again under its locks."""
    if not has_permission(user.id, "admin:manage_catalog", session):
        raise HTTPException(403, "Permission required: admin:manage_catalog")
    try:
        row = session.get(StudyType, UUID(str(study_type_id)))
    except ValueError:
        row = None
    if row is None or str(row.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Study type not found")
    refs = catalog_removal.references(session, "study_type", row.id)
    return CatalogTypeRemovalImpact(
        id=str(row.id), code=row.code, already_removed=row.removed_at is not None, references=refs,
        code_will_be_released=(row.code_released_at is not None) if row.removed_at is not None
        else not catalog_removal.has_history("study_type", refs),
        code_released=row.code_released_at is not None,
    )
