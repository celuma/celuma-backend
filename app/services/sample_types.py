"""Sample-type catalog helpers shared by registration, lists and the catalog API.

Contract: `celuma-engineering/specs/laboratory/sample-types.md`.
"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence
from uuid import UUID

from fastapi import HTTPException
from sqlmodel import Session, select

from app.models.audit import AuditLog
from app.models.report import ReportTemplate
from app.models.sample_type import SampleType
from app.schemas.sample_type import SampleTypeRef, SampleTypeResponse
from app.schemas.study_type import TemplateRef

#: Filter value meaning "samples without a catalog type" ("Sin tipo").
UNTYPED_FILTER_VALUE = "none"
#: Display text for a sample with no catalog type.
UNTYPED_LABEL = "Sin tipo"


def sample_type_ref(sample_type: Optional[SampleType]) -> Optional[SampleTypeRef]:
    if sample_type is None:
        return None
    return SampleTypeRef(
        id=str(sample_type.id), code=sample_type.code, name=sample_type.name, is_active=sample_type.is_active
    )


def sample_type_refs(session: Session, sample_type_ids: Iterable) -> dict:
    """`{sample_type_id: SampleTypeRef}` for the ids, in one query. Includes
    deactivated types: a referenced type stays readable."""
    ids = {i for i in sample_type_ids if i is not None}
    if not ids:
        return {}
    rows = session.exec(select(SampleType).where(SampleType.id.in_(ids))).all()
    return {row.id: sample_type_ref(row) for row in rows}


def ref_for_sample(session: Session, sample) -> Optional[SampleTypeRef]:
    if sample.sample_type_id is None:
        return None
    return sample_type_ref(session.get(SampleType, sample.sample_type_id))


def _parse_uuid(value) -> Optional[UUID]:
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def require_active_sample_types(session: Session, tenant_id, sample_type_ids: Sequence) -> dict:
    """Resolve the types a NEW sample may use, or raise.

    Every creation route calls this before it writes anything. A type of
    another laboratory is indistinguishable from a missing one (404, so
    catalog ids of other tenants cannot be probed); a deactivated type is a
    400 naming the type. Returns `{id_str: SampleType}`.
    """
    resolved: dict = {}
    for raw in dict.fromkeys(str(i) for i in sample_type_ids):
        uid = _parse_uuid(raw)
        row = session.get(SampleType, uid) if uid else None
        if row is None or str(row.tenant_id) != str(tenant_id):
            raise HTTPException(404, "Sample type not found")
        if not row.is_active:
            raise HTTPException(400, f"Sample type '{row.code}' is inactive and cannot be used for new samples")
        resolved[raw] = row
    return resolved


def template_ref_for(session: Session, sample_type: SampleType) -> Optional[TemplateRef]:
    if not sample_type.default_report_template_id:
        return None
    template = session.get(ReportTemplate, sample_type.default_report_template_id)
    return TemplateRef(id=str(template.id), name=template.name) if template else None


def sample_type_response(session: Session, sample_type: SampleType) -> SampleTypeResponse:
    return SampleTypeResponse(
        id=str(sample_type.id),
        tenant_id=str(sample_type.tenant_id),
        code=sample_type.code,
        name=sample_type.name,
        description=sample_type.description,
        is_active=sample_type.is_active,
        created_at=sample_type.created_at,
        default_report_template_id=(
            str(sample_type.default_report_template_id) if sample_type.default_report_template_id else None
        ),
        default_template=template_ref_for(session, sample_type),
    )


def record_audit(
    session: Session,
    *,
    tenant_id,
    user_id,
    action: str,
    entity_type: str,
    entity_id,
    description: Optional[str] = None,
    old_values: Optional[dict] = None,
    new_values: Optional[dict] = None,
    metadata: Optional[dict] = None,
) -> AuditLog:
    """Add (not commit) an `audit_log` row: it commits or rolls back with the
    change it describes."""
    row = AuditLog(
        tenant_id=tenant_id,
        actor_user_id=user_id,
        created_by=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        description=description,
        old_values=old_values,
        new_values=new_values,
        event_metadata=metadata,
    )
    session.add(row)
    return row


def snapshot(sample_type: SampleType) -> dict:
    return {
        "code": sample_type.code,
        "name": sample_type.name,
        "description": sample_type.description,
        "is_active": sample_type.is_active,
        "default_report_template_id": (
            str(sample_type.default_report_template_id) if sample_type.default_report_template_id else None
        ),
    }
