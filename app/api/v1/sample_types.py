"""Sample-type catalog: administration, JSON export/import and study-type copy.

Issue #17. Contracts: `celuma-engineering/specs/laboratory/sample-types.md`
(catalog) and `.../catalog-transfer.md` (export, import, copy). Administration
follows `study_types.py`: reading needs `lab:read`, every change, export,
import and copy needs `admin:manage_catalog`.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.api.v1.auth import AuthContext, current_user, get_auth_ctx
from app.core.db import get_session
from app.core.rbac import has_permission
from app.models.laboratory import Sample
from app.models.report import ReportTemplate
from app.models.sample_type import SampleType
from app.models.user import AppUser
from app.schemas.sample_type import (
    CopyConfirmRequest,
    CopyRequest,
    ImportConfirmRequest,
    ImportRequest,
    SampleTypeCreate,
    SampleTypeResponse,
    SampleTypesListResponse,
    SampleTypeUpdate,
    TransferPreviewResponse,
    TransferResult,
)
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
):
    """The tenant's sample types (requires lab:read). Registration asks for the
    active ones (default); administration passes `active_only=false`."""
    _require(user, "lab:read", session)
    query = select(SampleType).where(SampleType.tenant_id == ctx.tenant_id)
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


def _get_owned(session: Session, ctx: AuthContext, sample_type_id: str) -> SampleType:
    try:
        row = session.get(SampleType, sample_type_id)
    except Exception:
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
    if session.exec(select(SampleType).where(
        SampleType.tenant_id == ctx.tenant_id, SampleType.code == data.code
    )).first():
        raise HTTPException(400, f"Sample type with code '{data.code}' already exists")
    if data.default_report_template_id:
        _owned_template(session, ctx, data.default_report_template_id)
    row = SampleType(
        tenant_id=ctx.tenant_id, code=data.code, name=data.name, description=data.description,
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
    before = snapshot(row)
    if data.code is not None and data.code != row.code:
        if session.exec(select(SampleType).where(
            SampleType.tenant_id == ctx.tenant_id, SampleType.code == data.code, SampleType.id != row.id
        )).first():
            raise HTTPException(400, f"Sample type with code '{data.code}' already exists")
        row.code = data.code
    if data.default_report_template_id is not None:
        if data.default_report_template_id:
            _owned_template(session, ctx, data.default_report_template_id)
        row.default_report_template_id = data.default_report_template_id or None
    if data.name is not None:
        row.name = data.name
    if data.description is not None:
        row.description = data.description
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
    if hard_delete:
        in_use = session.exec(select(Sample.id).where(Sample.sample_type_id == row.id).limit(1)).first()
        if in_use is not None:
            raise HTTPException(409, "Sample type is referenced by samples; deactivate it instead")
        before = snapshot(row)
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
