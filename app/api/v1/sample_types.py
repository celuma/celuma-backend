"""Sample-type catalog: administration, JSON export/import and study-type copy.

Issue #17. Contracts: `celuma-engineering/specs/laboratory/sample-types.md`
(catalog) and `.../catalog-transfer.md` (export, import, copy). Administration
follows `study_types.py`: reading needs `lab:read`, every change, export,
import and copy needs `admin:manage_catalog`.
"""
import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.api.v1.auth import AuthContext, current_user, get_auth_ctx
from app.core.db import get_session
from app.core.rbac import has_permission
from app.models.laboratory import Sample
from app.models.report import ReportTemplate
from app.models.sample_type import SampleType
from app.models.user import AppUser
from app.schemas.study_type import CatalogTypeRemovalImpact, CatalogTypeRemoveRequest
from app.schemas.sample_type import (
    CopyConfirmRequest,
    CopyRequest,
    ImportConfirmRequest,
    ImportRequest,
    SampleTypeCreate,
    SampleTypeRemovalResponse,
    SampleTypeResponse,
    SampleTypesListResponse,
    SampleTypeUpdate,
    TransferPreviewResponse,
    TransferResult,
)
from app.services import catalog_removal
from app.services import catalog_transfer as transfer
from app.services.sample_types import record_audit, sample_type_response, snapshot

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sample-types")


def _require(user: AppUser, code: str, session: Session) -> None:
    if not has_permission(user.id, code, session):
        raise HTTPException(403, f"Permission required: {code}")


async def _bounded_body(request: Request) -> None:
    """Refuse an oversized import/copy body (413) before it is processed."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > transfer.MAX_BODY_BYTES:
        raise HTTPException(413, {"code": "body_too_large", "message": "The document is too large"})
    if len(await request.body()) > transfer.MAX_BODY_BYTES:
        raise HTTPException(413, {"code": "body_too_large", "message": "The document is too large"})


def _owned_template(session: Session, ctx: AuthContext, template_id: str) -> None:
    template = session.get(ReportTemplate, template_id)
    if not template:
        raise HTTPException(404, "Report template not found")
    if str(template.tenant_id) != ctx.tenant_id:
        raise HTTPException(403, "Report template does not belong to your tenant")


@router.get("/", response_model=SampleTypesListResponse)
def list_sample_types(
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
    active_only: bool = True,
    include_removed: bool = False,
):
    """The tenant's sample types (requires lab:read). Registration asks for the
    active ones (default); administration passes `active_only=false`.

    Céluma 1.5 (#94): a removed type is not part of the catalog and is left out
    unless `include_removed=true` (historical explanations and the removed-types
    view); it is never active, so `active_only=true` never returns it."""
    _require(user, "lab:read", session)
    query = select(SampleType).where(SampleType.tenant_id == ctx.tenant_id)
    if not include_removed:
        query = query.where(catalog_removal.not_removed(SampleType))
    if active_only:
        query = query.where(SampleType.is_active.is_(True))
    rows = session.exec(query.order_by(SampleType.code, SampleType.id)).all()
    return SampleTypesListResponse(sample_types=[sample_type_response(session, r) for r in rows])


@router.get("/export")
def export_sample_types(
    include_inactive: bool = True,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Download the whole sample-type catalog as versioned JSON
    (requires admin:manage_catalog). Read-only for the catalog."""
    _require(user, "admin:manage_catalog", session)
    document = transfer.build_export(session, ctx.tenant_id, "sample_types", include_inactive=include_inactive)
    record_audit(
        session, tenant_id=ctx.tenant_id, user_id=user.id, action="CATALOG.EXPORTED",
        entity_type="catalog_export", entity_id=ctx.tenant_id,
        description="Sample-type catalog exported",
        metadata={"catalog_kind": "sample_types", "item_count": document["item_count"],
                  "include_inactive": include_inactive},
    )
    session.commit()
    return JSONResponse(
        document,
        headers={"Content-Disposition": f'attachment; filename="{transfer.export_filename("sample_types")}"'},
    )


@router.post("/import/preview", response_model=TransferPreviewResponse,
             dependencies=[Depends(_bounded_body)])
def preview_import(
    body: ImportRequest,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Validate an exported study-type or sample-type document against this
    tenant's sample-type catalog. Writes nothing."""
    _require(user, "admin:manage_catalog", session)
    return transfer.plan_import(session, ctx.tenant_id, body.document).preview()


@router.post("/import/confirm", response_model=TransferResult, dependencies=[Depends(_bounded_body)])
def confirm_import(
    body: ImportConfirmRequest,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Create the selected records, all or nothing (see the spec for the
    conflict policy)."""
    _require(user, "admin:manage_catalog", session)
    return transfer.confirm(
        session, tenant_id=ctx.tenant_id, user_id=user.id,
        build=lambda: transfer.plan_import(session, ctx.tenant_id, body.document),
        selected=body.selected_indexes, on_conflict=body.on_conflict, plan_digest=body.plan_digest,
        source_label={"source_document_sha256": transfer.document_sha256(body.document)},
    )


@router.post("/copy-from-study-types/preview", response_model=TransferPreviewResponse)
def preview_copy(
    body: CopyRequest,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Preview copying the selected study types into sample types. Writes nothing."""
    _require(user, "admin:manage_catalog", session)
    return transfer.plan_copy(session, ctx.tenant_id, body.study_type_ids).preview()


@router.post("/copy-from-study-types/confirm", response_model=TransferResult)
def confirm_copy(
    body: CopyConfirmRequest,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Copy the selected study types into new, independent sample types. The
    study types are not modified."""
    _require(user, "admin:manage_catalog", session)
    return transfer.confirm(
        session, tenant_id=ctx.tenant_id, user_id=user.id,
        build=lambda: transfer.plan_copy(session, ctx.tenant_id, body.study_type_ids),
        selected=None, on_conflict=body.on_conflict, plan_digest=body.plan_digest,
        source_label={"source_study_type_ids": list(dict.fromkeys(body.study_type_ids))},
    )


def _code_taken_message(code: str, clash: SampleType) -> str:
    if clash.removed_at is not None:
        return (f"Sample type with code '{code}' already exists: the code belongs to a type removed from the "
                "catalog and stays reserved because samples reference it")
    return f"Sample type with code '{code}' already exists"


def _get_owned(session: Session, ctx: AuthContext, sample_type_id: str) -> SampleType:
    # A malformed id is 404 like an unknown one; parsed first so no failing query aborts the transaction.
    try:
        row = session.get(SampleType, UUID(str(sample_type_id)))
    except ValueError:
        row = None
    if not row or str(row.tenant_id) != ctx.tenant_id:
        raise HTTPException(404, "Sample type not found")
    return row


@router.get("/{sample_type_id}", response_model=SampleTypeResponse)
def get_sample_type(
    sample_type_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    _require(user, "lab:read", session)
    return sample_type_response(session, _get_owned(session, ctx, sample_type_id))


@router.post("/", response_model=SampleTypeResponse)
def create_sample_type(
    data: SampleTypeCreate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Create a sample type (requires admin:manage_catalog)."""
    _require(user, "admin:manage_catalog", session)
    # #94: a code released by an unused removed type is free; one kept by history is not.
    catalog_removal.lock_catalog_codes(session, "sample_type", ctx.tenant_id)
    clash = session.exec(select(SampleType).where(
        SampleType.tenant_id == ctx.tenant_id, SampleType.code == data.code, catalog_removal.reserves_code(SampleType)
    )).first()
    if clash:
        raise HTTPException(400, _code_taken_message(data.code, clash))
    if data.default_report_template_id:
        _owned_template(session, ctx, data.default_report_template_id)
    row = SampleType(
        tenant_id=ctx.tenant_id, code=data.code, name=data.name, description=(data.description or "").strip() or None,
        is_active=data.is_active if data.is_active is not None else True,
        default_report_template_id=data.default_report_template_id,
    )
    session.add(row)
    try:
        session.flush()
        record_audit(session, tenant_id=ctx.tenant_id, user_id=user.id, action="SAMPLE_TYPE.CREATED",
                     entity_type="sample_type", entity_id=row.id, new_values=snapshot(row))
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(400, f"Sample type with code '{data.code}' already exists")
    session.refresh(row)
    logger.info(f"Sample type '{row.code}' created",
                extra={"event": "sample_type.created", "sample_type_id": str(row.id), "user_id": str(user.id)})
    return sample_type_response(session, row)


@router.put("/{sample_type_id}", response_model=SampleTypeResponse)
def update_sample_type(
    sample_type_id: str,
    data: SampleTypeUpdate,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Update a sample type (requires admin:manage_catalog). Samples keep
    pointing at it: renaming changes how they display, nothing else."""
    _require(user, "admin:manage_catalog", session)
    row = _get_owned(session, ctx, sample_type_id)
    # Céluma 1.5 (#94): a removed type is frozen; an ordinary edit can neither change nor reactivate it.
    catalog_removal.ensure_not_removed(row, "sample_type", purpose="or edited")
    before = snapshot(row)
    if data.code is not None and data.code != row.code:
        catalog_removal.lock_catalog_codes(session, "sample_type", ctx.tenant_id)
        clash = session.exec(select(SampleType).where(
            SampleType.tenant_id == ctx.tenant_id, SampleType.code == data.code, SampleType.id != row.id,
            catalog_removal.reserves_code(SampleType),
        )).first()
        if clash:
            raise HTTPException(400, _code_taken_message(data.code, clash))
        row.code = data.code
    # Céluma 1.4.1: the template drives the report defaults of the type's orders, so it must be clearable from
    # the UI. Same semantics as `description`: omitted = unchanged; an explicit null or blank clears it.
    if "default_report_template_id" in data.model_fields_set:
        if data.default_report_template_id:
            _owned_template(session, ctx, data.default_report_template_id)
        row.default_report_template_id = data.default_report_template_id or None
    if data.name is not None:
        row.name = data.name
    # `description` is the one field that can be emptied. Omitted = unchanged;
    # an explicit null or blank string clears it (stored as NULL). Pydantic
    # cannot tell omitted from null by value, so `model_fields_set` decides.
    if "description" in data.model_fields_set:
        row.description = (data.description or "").strip() or None
    if data.is_active is not None:
        row.is_active = data.is_active
    session.add(row)
    try:
        session.flush()
        record_audit(session, tenant_id=ctx.tenant_id, user_id=user.id, action="SAMPLE_TYPE.UPDATED",
                     entity_type="sample_type", entity_id=row.id, old_values=before, new_values=snapshot(row))
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(400, f"Sample type with code '{data.code}' already exists")
    session.refresh(row)
    logger.info(f"Sample type '{row.code}' updated",
                extra={"event": "sample_type.updated", "sample_type_id": str(row.id), "user_id": str(user.id)})
    return sample_type_response(session, row)


@router.delete("/{sample_type_id}")
def delete_sample_type(
    sample_type_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
    hard_delete: bool = False,
):
    """Deactivate a sample type (default), or delete it permanently when no
    sample references it (requires admin:manage_catalog)."""
    _require(user, "admin:manage_catalog", session)
    row = _get_owned(session, ctx, sample_type_id)
    # Céluma 1.5 (#94): a removed type is retained on purpose (its code stays reserved); neither deactivating
    # it again nor erasing it is an ordinary catalog action.
    catalog_removal.ensure_not_removed(row, "sample_type", purpose="or deleted")
    if hard_delete:
        in_use = session.exec(select(Sample.id).where(Sample.sample_type_id == row.id).limit(1)).first()
        if in_use is not None:
            raise HTTPException(409, "Sample type is referenced by samples; deactivate it instead")
        before = snapshot(row)
        # Céluma 1.5 (#64): a type's own numbering goes with it (the row has no FK; counters and ledger stay).
        session.execute(
            text("DELETE FROM numbering_type_config WHERE tenant_id = :t AND entity_kind = :k AND type_id = :i"),
            {"t": str(row.tenant_id), "k": "sample", "i": str(row.id)},
        )
        session.delete(row)
        record_audit(session, tenant_id=ctx.tenant_id, user_id=user.id, action="SAMPLE_TYPE.DELETED",
                     entity_type="sample_type", entity_id=row.id, old_values=before)
        session.commit()
        return {"message": "Sample type permanently deleted", "id": sample_type_id}
    before = snapshot(row)
    row.is_active = False
    session.add(row)
    record_audit(session, tenant_id=ctx.tenant_id, user_id=user.id, action="SAMPLE_TYPE.DEACTIVATED",
                 entity_type="sample_type", entity_id=row.id, old_values=before, new_values=snapshot(row))
    session.commit()
    return {"message": "Sample type deactivated", "id": str(row.id)}


@router.post("/{sample_type_id}/remove", response_model=SampleTypeRemovalResponse)
def remove_sample_type(
    sample_type_id: str,
    body: CatalogTypeRemoveRequest | None = None,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """Remove a sample type from the catalog (requires admin:manage_catalog; Céluma 1.5, #94).

    A retained tombstone, distinct from deactivation: the row, id and code stay, every sample keeps its
    reference and shows the type as removed; the type disappears from the catalog and every selector, and is
    refused for new samples, transfers, edits and numbering changes. Referenced or not, the type is kept
    (no data is erased). Its code is RELEASED for another type only when no sample, withdrawn ones included,
    references it (owner decision 2026-11-25); otherwise it stays reserved. Repeating the request answers the
    same removal and writes nothing.
    """
    _require(user, "admin:manage_catalog", session)
    row = _get_owned(session, ctx, sample_type_id)
    reason = catalog_removal.clean_reason(body.reason if body else None)
    # Code lock first (create/rename/import of this catalog), then the row: serialises with concurrent
    # removals and with registrations that hold the type FOR SHARE, so the count below is final.
    catalog_removal.lock_catalog_codes(session, "sample_type", ctx.tenant_id)
    session.refresh(row, with_for_update=True)
    refs = catalog_removal.references(session, "sample_type", row.id)
    release = not catalog_removal.has_history("sample_type", refs)
    if row.removed_at is not None:
        return SampleTypeRemovalResponse(
            **sample_type_response(session, row).model_dump(), already_removed=True, references=refs)
    before = snapshot(row)
    catalog_removal.mark_removed(row, user_id=user.id, reason=reason, release_code=release)
    session.add(row)
    record_audit(session, tenant_id=ctx.tenant_id, user_id=user.id, action="SAMPLE_TYPE.REMOVED",
                 entity_type="sample_type", entity_id=row.id, old_values=before, new_values=snapshot(row),
                 description=f"Sample type '{row.code}' removed from the catalog"
                 + ("; code released" if release else "; code kept reserved by its samples"),
                 metadata={"references": refs, "was_active": before["is_active"], "code_released": release})
    session.commit()
    session.refresh(row)
    logger.info(f"Sample type '{row.code}' removed",
                extra={"event": "sample_type.removed", "sample_type_id": str(row.id), "user_id": str(user.id)})
    return SampleTypeRemovalResponse(**sample_type_response(session, row).model_dump(), references=refs)


@router.get("/{sample_type_id}/removal-impact", response_model=CatalogTypeRemovalImpact)
def sample_type_removal_impact(
    sample_type_id: str,
    session: Session = Depends(get_session),
    ctx: AuthContext = Depends(get_auth_ctx),
    user: AppUser = Depends(current_user),
):
    """What removing this sample type would do now (requires admin:manage_catalog; #94). Reads only;
    the removal itself decides again under its locks."""
    if not has_permission(user.id, "admin:manage_catalog", session):
        raise HTTPException(403, "Permission required: admin:manage_catalog")
    row = _get_owned(session, ctx, sample_type_id)
    refs = catalog_removal.references(session, "sample_type", row.id)
    return CatalogTypeRemovalImpact(
        id=str(row.id), code=row.code, already_removed=row.removed_at is not None, references=refs,
        code_will_be_released=(row.code_released_at is not None) if row.removed_at is not None
        else not catalog_removal.has_history("sample_type", refs),
        code_released=row.code_released_at is not None,
    )
