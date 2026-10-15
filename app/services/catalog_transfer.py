"""Catalog JSON export, import into sample types, and direct copy (issue #17).

One validation and transfer pipeline serves both entry points:

* `POST /sample-types/import/*` feeds it a parsed export document;
* `POST /sample-types/copy-from-study-types/*` builds the same item list from
  the caller's own study types, with no file round trip.

Contract: `celuma-engineering/specs/laboratory/catalog-transfer.md`.

Nothing here trusts the document. Tenant ownership is always the
authenticated tenant's; source ids (`provenance.source_id`) and template ids
are provenance/candidates, never identities or authorization; an existing
destination row is never modified.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Sequence
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models.report import ReportTemplate
from app.models.sample_type import SampleType
from app.models.study_type import StudyType
from app.schemas.sample_type import (
    MAX_DESCRIPTION_LENGTH,
    SkippedItem,
    TransferIssue,
    TransferItem,
    TransferPreviewResponse,
    TransferResult,
)
from app.services.sample_types import record_audit, sample_type_response, snapshot

SCHEMA_ID = "celuma.catalog-export"
SCHEMA_VERSION = 1
KINDS = ("study_types", "sample_types")

#: Resource bounds. A document over any of them is refused whole.
MAX_ITEMS = 500
MAX_BODY_BYTES = 1024 * 1024
MAX_SOURCE_ID_LENGTH = 64
MAX_TEMPLATE_NAME_LENGTH = 255

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ITEM_KEYS = {"code", "name", "description", "is_active", "provenance"}


def _refuse(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_filename(kind: str, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"celuma-{kind.replace('_', '-')}-export-{now:%Y%m%d}.json"


def build_export(session: Session, tenant_id, kind: str, *, include_inactive: bool = True) -> dict:
    """The full catalog of `kind` for the tenant, whatever its size (no paging).

    Deterministic order (code, then id). Inactive entries are included unless
    excluded, flagged by `is_active`; an empty catalog is a valid document
    with `items: []`. The tenant's id is NOT part of the document.
    """
    model = StudyType if kind == "study_types" else SampleType
    stmt = select(model).where(model.tenant_id == tenant_id)
    if not include_inactive:
        stmt = stmt.where(model.is_active.is_(True))
    rows = session.exec(stmt.order_by(model.code, model.id)).all()
    template_ids = {r.default_report_template_id for r in rows if r.default_report_template_id}
    names = {}
    if template_ids:
        names = {
            t.id: t.name
            for t in session.exec(select(ReportTemplate).where(ReportTemplate.id.in_(template_ids))).all()
        }
    items = []
    for r in rows:
        provenance: dict[str, Any] = {"source_id": str(r.id)}
        if r.default_report_template_id:
            provenance["default_report_template_id"] = str(r.default_report_template_id)
            provenance["default_report_template_name"] = names.get(r.default_report_template_id)
        items.append({
            "code": r.code,
            "name": r.name,
            "description": r.description,
            "is_active": r.is_active,
            "provenance": provenance,
        })
    return {
        "schema": SCHEMA_ID,
        "version": SCHEMA_VERSION,
        "catalog_kind": kind,
        "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "include_inactive": include_inactive,
        "item_count": len(items),
        "items": items,
    }


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass
class _Plan:
    source_kind: str
    origin: str
    items: list[TransferItem] = field(default_factory=list)
    #: Normalised destination fields per item index, for creatable items.
    payloads: dict[int, dict] = field(default_factory=dict)

    def digest(self) -> str:
        canonical = json.dumps(
            [[i.index, i.status, i.code, i.name, i.description, i.is_active, i.template_reference,
              i.existing_sample_type_id, str(self.payloads.get(i.index, {}).get("default_report_template_id"))]
             for i in self.items],
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def preview(self) -> TransferPreviewResponse:
        ready = sum(1 for i in self.items if i.status == "ready")
        invalid = sum(1 for i in self.items if i.status == "invalid")
        return TransferPreviewResponse(
            source_kind=self.source_kind,  # type: ignore[arg-type]
            origin=self.origin,  # type: ignore[arg-type]
            total=len(self.items),
            ready=ready,
            conflicts=len(self.items) - ready - invalid,
            invalid=invalid,
            plan_digest=self.digest(),
            items=self.items,
        )


def validate_document(document: Any) -> tuple[str, list[dict]]:
    """Check the envelope; returns `(catalog_kind, raw items)` or raises 422."""
    if not isinstance(document, dict):
        raise _refuse(422, "invalid_document", "The document must be a JSON object")
    if document.get("schema") != SCHEMA_ID:
        raise _refuse(422, "unsupported_schema", f"Not a Céluma catalog export (expected schema '{SCHEMA_ID}')")
    version = document.get("version")
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise _refuse(422, "unsupported_version", f"Unsupported export version {version!r}; supported: {SCHEMA_VERSION}")
    kind = document.get("catalog_kind")
    if kind not in KINDS:
        raise _refuse(422, "unsupported_catalog_kind", f"catalog_kind must be one of {', '.join(KINDS)}")
    items = document.get("items")
    if not isinstance(items, list):
        raise _refuse(422, "invalid_document", "'items' must be a list")
    if len(items) > MAX_ITEMS:
        raise _refuse(413, "too_many_items", f"A document may hold at most {MAX_ITEMS} items (got {len(items)})")
    return kind, items


def _issue(code: str, message: str, field_name: Optional[str] = None) -> TransferIssue:
    return TransferIssue(code=code, message=message, field=field_name)


def _text(value: Any, name: str, issues: list, *, required: bool, limit: int, multiline: bool = False) -> Optional[str]:
    if value is None:
        if required:
            issues.append(_issue("required", f"{name} is required", name))
        return None
    if not isinstance(value, str):
        issues.append(_issue("invalid_type", f"{name} must be a string", name))
        return None
    value = value.strip()
    if _CONTROL.search(value) or (not multiline and re.search(r"[\r\n\t]", value)):
        issues.append(_issue("invalid_characters", f"{name} contains control characters", name))
        return None
    if required and not value:
        issues.append(_issue("required", f"{name} cannot be empty", name))
        return None
    if len(value) > limit:
        issues.append(_issue("too_long", f"{name} cannot exceed {limit} characters", name))
        return None
    return value or None


def _normalise_item(index: int, raw: Any) -> tuple[TransferItem, dict]:
    issues: list[TransferIssue] = []
    warnings: list[TransferIssue] = []
    item = TransferItem(index=index, status="invalid")
    if not isinstance(raw, dict):
        item.issues = [_issue("invalid_item", "Item must be an object")]
        return item, {}
    unknown = sorted(set(raw) - _ITEM_KEYS)
    if unknown:
        warnings.append(_issue("unknown_field_ignored", f"Ignored fields: {', '.join(map(str, unknown))}"))

    code = _text(raw.get("code"), "code", issues, required=True, limit=50)
    name = _text(raw.get("name"), "name", issues, required=True, limit=255)
    description = _text(raw.get("description"), "description", issues, required=False,
                        limit=MAX_DESCRIPTION_LENGTH, multiline=True)
    is_active = raw.get("is_active", True)
    if not isinstance(is_active, bool):
        issues.append(_issue("invalid_type", "is_active must be a boolean", "is_active"))
        is_active = True
    provenance = raw.get("provenance")
    if provenance is not None and not isinstance(provenance, dict):
        issues.append(_issue("invalid_type", "provenance must be an object", "provenance"))
        provenance = None
    provenance = provenance or {}
    source_id = provenance.get("source_id")
    if source_id is not None and not (isinstance(source_id, str) and len(source_id) <= MAX_SOURCE_ID_LENGTH):
        warnings.append(_issue("provenance_ignored", "provenance.source_id is not a short string; ignored"))
        source_id = None

    item.code = code.upper() if code else None
    item.name = name
    item.description = description
    item.is_active = is_active
    item.source_id = source_id
    item.issues = issues
    item.warnings = warnings
    candidate = provenance.get("default_report_template_id")
    return item, {"template_candidate": candidate if isinstance(candidate, str) else None}


def _resolve_templates(session: Session, tenant_id, plan: _Plan, candidates: dict[int, Optional[str]]) -> None:
    """Map each template candidate to a template of THIS tenant, or drop it.

    The id in a document is only a candidate. A template that does not exist,
    or belongs to another laboratory, is dropped with the same warning, so
    nothing is learnt about other tenants' templates.
    """
    parsed: dict[int, UUID] = {}
    for index, cand in candidates.items():
        if cand is None:
            continue
        try:
            parsed[index] = UUID(cand)
        except ValueError:
            continue
    valid: set = set()
    if parsed:
        valid = set(session.exec(
            select(ReportTemplate.id).where(
                ReportTemplate.tenant_id == tenant_id, ReportTemplate.id.in_(set(parsed.values()))
            )
        ).all())
    for item in plan.items:
        cand = candidates.get(item.index)
        if cand is None:
            continue
        uid = parsed.get(item.index)
        if uid is not None and uid in valid:
            item.template_reference = "mapped"
            plan.payloads.setdefault(item.index, {})["default_report_template_id"] = uid
        else:
            item.template_reference = "dropped"
            item.warnings.append(_issue(
                "template_reference_dropped",
                "The report-template reference does not match a template of this laboratory; "
                "the sample type is created without one",
            ))


def _build_plan(session: Session, tenant_id, source_kind: str, origin: str, raw_items: Sequence[Any]) -> _Plan:
    plan = _Plan(source_kind=source_kind, origin=origin)
    candidates: dict[int, Optional[str]] = {}
    for index, raw in enumerate(raw_items):
        item, extra = _normalise_item(index, raw)
        plan.items.append(item)
        candidates[index] = extra.get("template_candidate")
        if not item.issues:
            plan.payloads[index] = {
                "code": item.code, "name": item.name, "description": item.description, "is_active": item.is_active,
            }
    _resolve_templates(session, tenant_id, plan, candidates)

    codes = [i.code for i in plan.items if not i.issues and i.code]
    existing = {}
    if codes:
        existing = {
            r.code: r for r in session.exec(
                select(SampleType).where(SampleType.tenant_id == tenant_id, SampleType.code.in_(set(codes)))
            ).all()
        }
    seen: set = set()
    for item in plan.items:
        if item.issues:
            item.status = "invalid"
            plan.payloads.pop(item.index, None)
            continue
        if item.code in seen:
            item.status = "duplicate_in_source"
            item.issues.append(_issue("duplicate_in_source", f"Code '{item.code}' appears earlier in the source", "code"))
        elif item.code in existing:
            row = existing[item.code]
            item.status = "code_exists"
            item.existing_sample_type_id = str(row.id)
            item.issues.append(_issue(
                "code_exists", f"A sample type with code '{item.code}' already exists"
                + ("" if row.is_active else " (inactive)"), "code"))
        else:
            item.status = "ready"
        seen.add(item.code)
        if item.status != "ready":
            plan.payloads.pop(item.index, None)
    # name reuse is informative only (names are not unique)
    names = {i.name.casefold() for i in plan.items if i.status == "ready" and i.name}
    if names:
        clash = session.exec(
            select(SampleType.name).where(SampleType.tenant_id == tenant_id)
        ).all()
        clash_names = {n.casefold() for n in clash}
        for item in plan.items:
            if item.status == "ready" and item.name and item.name.casefold() in clash_names:
                item.warnings.append(_issue("name_exists", "A sample type with this name already exists", "name"))
    return plan


def plan_import(session: Session, tenant_id, document: Any) -> _Plan:
    kind, items = validate_document(document)
    return _build_plan(session, tenant_id, kind, "file", items)


def plan_copy(session: Session, tenant_id, study_type_ids: Sequence[str]) -> _Plan:
    """The same plan, built from the caller's own study types."""
    if not study_type_ids:
        raise _refuse(422, "empty_selection", "Select at least one study type")
    if len(study_type_ids) > MAX_ITEMS:
        raise _refuse(413, "too_many_items", f"At most {MAX_ITEMS} study types per copy")
    ids: list[UUID] = []
    for raw in dict.fromkeys(study_type_ids):
        try:
            ids.append(UUID(str(raw)))
        except ValueError:
            raise _refuse(422, "invalid_id", f"Invalid study type id: {raw}")
    rows = {
        r.id: r for r in session.exec(
            select(StudyType).where(StudyType.tenant_id == tenant_id, StudyType.id.in_(ids))
        ).all()
    }
    if len(rows) != len(ids):  # unknown, or another laboratory's: indistinguishable
        raise _refuse(404, "study_type_not_found", "One or more study types were not found")
    raw_items = []
    for uid in ids:
        r = rows[uid]
        provenance: dict[str, Any] = {"source_id": str(r.id)}
        if r.default_report_template_id:
            provenance["default_report_template_id"] = str(r.default_report_template_id)
        raw_items.append({
            "code": r.code, "name": r.name, "description": r.description,
            "is_active": r.is_active, "provenance": provenance,
        })
    plan = _build_plan(session, tenant_id, "study_types", "copy", raw_items)
    for item in plan.items:
        if item.status != "invalid" and not item.is_active:
            item.warnings.append(_issue(
                "source_inactive", "The study type is inactive; the sample type is created inactive too"))
    return plan


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


def _lock_tenant_transfers(session: Session, tenant_id) -> None:
    """Serialise transfers of one tenant (PostgreSQL); the unique constraint
    on (tenant_id, code) remains the final guard."""
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"sample_type_transfer:{tenant_id}"},
        )


def _stale(plan: _Plan) -> HTTPException:
    return HTTPException(409, {
        "code": "preview_stale",
        "message": "The plan changed since the preview; review the new preview",
        "preview": plan.preview().model_dump(),
    })


def _conflict_refusal(plan: _Plan, indexes: Iterable[int]) -> HTTPException:
    return HTTPException(409, {
        "code": "conflicts",
        "message": "Selected records conflict with the destination catalog; nothing was created",
        "indexes": sorted(indexes),
        "preview": plan.preview().model_dump(),
    })


def confirm(
    session: Session,
    *,
    tenant_id,
    user_id,
    build: "callable",
    selected: Optional[Sequence[int]],
    on_conflict: str,
    plan_digest: str,
    source_label: dict,
) -> TransferResult:
    """Revalidate the plan under the tenant's transfer lock and create the
    selected records in ONE transaction.

    `build()` returns a fresh plan (it re-reads the destination). All-or-
    nothing: any failure rolls everything back. Existing rows are never
    changed. `selected=None` (copy) means every item of the plan.
    """
    _lock_tenant_transfers(session, tenant_id)
    plan = build()
    if plan.digest() != plan_digest:
        session.rollback()
        raise _stale(plan)

    indexes = list(dict.fromkeys(selected)) if selected is not None else [i.index for i in plan.items]
    if not indexes:
        raise _refuse(422, "empty_selection", "Select at least one record")
    by_index = {i.index: i for i in plan.items}
    unknown = [i for i in indexes if i not in by_index]
    if unknown:
        raise _refuse(422, "unknown_index", f"Unknown record indexes: {unknown}")
    invalid = [i for i in indexes if by_index[i].status == "invalid"]
    if invalid:
        raise _refuse(422, "invalid_selection", f"Invalid records cannot be imported: {sorted(invalid)}")
    conflicting = [i for i in indexes if by_index[i].status != "ready"]
    if conflicting and on_conflict == "abort":
        session.rollback()
        raise _conflict_refusal(plan, conflicting)

    operation_id = uuid4()
    created, skipped = [], []
    try:
        for index in indexes:
            item = by_index[index]
            if item.status != "ready":
                skipped.append(SkippedItem(index=index, code=item.code, reason=item.status))
                continue
            payload = plan.payloads[index]
            row = SampleType(tenant_id=tenant_id, **payload)
            session.add(row)
            session.flush()
            created.append((row, item))
            record_audit(
                session, tenant_id=tenant_id, user_id=user_id,
                action="SAMPLE_TYPE.COPIED_FROM_STUDY_TYPE" if plan.origin == "copy" else "SAMPLE_TYPE.IMPORTED",
                entity_type="sample_type", entity_id=row.id,
                description=f"Sample type '{row.code}' created by {plan.origin} transfer",
                new_values=snapshot(row),
                metadata={
                    "operation_id": str(operation_id), "origin": plan.origin, "source_kind": plan.source_kind,
                    "source_id": item.source_id, "source_index": index, **source_label,
                },
            )
        if created:
            record_audit(
                session, tenant_id=tenant_id, user_id=user_id,
                action="SAMPLE_TYPE.TRANSFER_COMPLETED", entity_type="sample_type_transfer", entity_id=operation_id,
                description=f"{len(created)} sample type(s) created, {len(skipped)} skipped",
                metadata={
                    "operation_id": str(operation_id), "origin": plan.origin, "source_kind": plan.source_kind,
                    "on_conflict": on_conflict, "selected": len(indexes), "created": len(created),
                    "skipped": [s.model_dump() for s in skipped], "plan_digest": plan.digest(), **source_label,
                },
            )
        session.commit()
    except IntegrityError:
        # A concurrent writer took a code between validation and insert (the
        # lock only serialises transfers, not manual creation).
        session.rollback()
        raise HTTPException(409, {
            "code": "concurrent_conflict",
            "message": "A sample type code was taken concurrently; nothing was created. Retry from the preview",
        })
    except Exception:
        session.rollback()
        raise
    for row, _item in created:
        session.refresh(row)
    return TransferResult(
        operation_id=str(operation_id),
        created=[sample_type_response(session, row) for row, _ in created],
        skipped=skipped,
    )


def document_sha256(document: Any) -> str:
    return hashlib.sha256(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
