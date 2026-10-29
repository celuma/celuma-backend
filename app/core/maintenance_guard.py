"""Writers honor a laboratory in cleanup maintenance (Céluma 1.4, issue #39; ADR 0021, E4).

A cleanup-only lock that writers never take protects nothing, so the guard lives where
every writer passes: the ORM unit of work, the same interception point as the global
removed-record filter (`app/core/soft_removal.py`). Request handlers, the PDF generation
service and any worker that writes through an ORM `Session` are covered by construction.

When a flush inserts, updates or deletes an operational row of a tenant, the first time
that tenant is seen in the transaction we

1. take the transaction-scoped SHARED advisory lock `lab-cleanup:<tenant>` (held until
   commit/rollback), then
2. read whether the tenant has an OPEN cleanup operation whose database phase has not yet
   committed. If so the write is refused with `423 laboratory_cleanup_in_progress` before
   any SQL is emitted.

The executor raises the gate by COMMITTING the operation row and then runs a barrier: a
short transaction taking the same lock EXCLUSIVELY. The barrier returns only after every
writer that started before the gate has finished; later writers queue behind it and then
read the gate. The destructive transaction does not hold this lock while it waits on row
locks, which could otherwise deadlock with a writer holding an order lock.

The executor's own sessions set `session.info[EXECUTOR_KEY]` and are not guarded.

Known limit (documented in the spec): a future raw-SQL writer on an operational table would
bypass this listener. `tests/test_laboratory_cleanup_guard.py` enumerates the guarded models
against every table that references `order`/`sample`/`report`, so adding a table without
deciding about it fails a test.
"""
from __future__ import annotations

import json
import logging
from typing import Dict, Iterable, Optional, Set, Tuple
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import event, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as _OrmSession

from app.models.assignment import Assignment
from app.models.billing import Invoice, InvoiceItem, Payment
from app.models.events import OrderEvent
from app.models.laboratory import (
    Order,
    OrderComment,
    OrderCommentMention,
    OrderLabel,
    Sample,
    SampleImage,
    SampleLabel,
)
from app.models.order_management import OrderRequestingPhysician
from app.models.report import Report, ReportTemplate, ReportVersion
from app.models.report_amendment import ReportAmendment
from app.models.report_letterhead_version import ReportLetterheadVersion
from app.models.report_review import ReportReview
from app.models.report_template_version import ReportTemplateVersion
from app.models.storage import SampleImageRendition, StorageObject
from app.models.tenant import Tenant
from app.models.user import AppUser

logger = logging.getLogger(__name__)

EXECUTOR_KEY = "laboratory_cleanup_executor"
_CHECKED_KEY = "_laboratory_cleanup_checked"

IN_PROGRESS_CODE = "laboratory_cleanup_in_progress"
IN_PROGRESS_MESSAGE = (
    "El laboratorio está en mantenimiento: se está limpiando su contenido operativo. "
    "Vuelve a intentarlo cuando termine."
)

#: Operational models that carry their own `tenant_id`.
TENANT_MODELS = (
    Order, Sample, SampleImage, OrderComment, OrderEvent, OrderRequestingPhysician,
    Report, ReportAmendment, ReportReview, Assignment, Invoice, InvoiceItem, Payment,
)
#: Operational models without `tenant_id`: (model, parent table, FK attribute on the model).
PARENT_MODELS: Dict[type, Tuple[str, str]] = {
    ReportVersion: ("report", "report_id"),
    SampleImageRendition: ("sample_image", "sample_image_id"),
    OrderLabel: ('"order"', "order_id"),
    SampleLabel: ("sample", "sample_id"),
    OrderCommentMention: ("order_comment", "comment_id"),
}

#: Every guarded model — also what the guard test compares against the FK graph.
GUARDED_MODELS = tuple(TENANT_MODELS) + tuple(PARENT_MODELS)

#: Every ORM writer that can PERSIST a reference to a managed object by key (any laboratory), with the attribute
#: that carries it: configuration JSON, a surviving report's template snapshot, public URL columns, and a new
#: `storage_object` row for the same key. Writers of these take the global asset lock (shared) from the first
#: flush that changes the attribute until they commit, and refuse to persist a reference to an object a cleanup
#: has scheduled for deletion or already deleted. See `lock_assets_exclusive` and `ASSET_PROTOCOL`.
REFERENCE_ATTRIBUTES: Dict[type, Tuple[str, ...]] = {
    ReportTemplate: ("template_json",),
    ReportTemplateVersion: ("configuration",),
    ReportLetterheadVersion: ("configuration",),
    Report: ("template",),
    Tenant: ("logo_url",),
    AppUser: ("avatar_url",),
    StorageObject: ("object_key",),
}
CONFIG_MODELS = tuple(REFERENCE_ATTRIBUTES)

#: Outbox states in which a NEW reference to the key is refused: the object is scheduled (PENDING/FAILED) or gone
#: (DELETED/ABSENT). KEPT is not listed: a referenced object is never deleted, so a reference to it is fine.
REFUSED_OBJECT_STATUSES = "('PENDING','FAILED','DELETED','ABSENT')"

ASSET_LOCK_KEY = "lab-cleanup-assets"
ASSET_PROTOCOL = """Reference / physical-deletion protocol (one global advisory lock, `ASSET_LOCK_KEY`):
  * a WRITER that persists a reference (REFERENCE_ATTRIBUTES) takes the lock SHARED at its first such flush, holds it
    until it commits, and refuses keys whose outbox row is PENDING/FAILED/DELETED/ABSENT;
  * the cleanup's DATABASE phase holds it EXCLUSIVE while it resolves objects and writes the outbox;
  * the cleanup's OBJECT phase holds it EXCLUSIVE, per object, across [reference re-check -> HEAD -> DELETE -> verify].
Hence for any key either the reference committed first (the re-check sees it, the object is KEPT) or the deletion
holds the lock first (the writer waits, then finds the row scheduled/deleted and is refused): never both
successful with the bytes gone. Lock order: no holder of the exclusive lock waits for a tenant lock. Uncoordinated:
writers that bypass the ORM (hand-written SQL, scripts) are only covered by the re-check, not serialized."""
ASSET_SCHEDULED_CODE = "asset_scheduled_for_deletion"

_OPEN_GATE_SQL = text(
    "SELECT 1 FROM laboratory_cleanup_operation "
    "WHERE tenant_id = :t AND status IN ('QUEUED','RUNNING','FAILED') "
    "AND database_cleaned_at IS NULL LIMIT 1"
)


def lock_key(tenant_id) -> str:
    return f"lab-cleanup:{tenant_id}"


def gate_is_up(session, tenant_id) -> bool:
    """Whether the tenant has an open operation whose database phase has not committed.
    Raw SQL on purpose: no ORM filter or identity-map state is involved."""
    return session.execute(_OPEN_GATE_SQL, {"t": str(tenant_id)}).first() is not None


def lock_assets_exclusive(session, timeout_seconds: Optional[int] = None) -> None:
    """The cleanup's database phase takes this for its whole transaction, BEFORE it resolves which objects are
    exclusively owned. Preserved-configuration writers hold the same lock SHARED from their first flush until they
    commit, so (a) a reference committed before the resolution is visible to it and keeps the object, and (b) a
    writer arriving later waits for the cleanup's commit and then finds the key scheduled (outbox row) and is
    refused. It is global (not per tenant) because a template of ANY laboratory may name an object of the target."""
    if _is_postgres(session):
        if timeout_seconds is not None:
            session.execute(text(f"SET LOCAL lock_timeout = '{int(timeout_seconds) * 1000}ms'"))
        try:
            session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": ASSET_LOCK_KEY})
        except OperationalError as exc:  # lock_timeout: retryable, nothing was deleted
            session.rollback()
            raise TimeoutError("asset lock timed out") from exc


def _changed(session, obj, attribute: str) -> bool:
    if obj in session.new:
        return True
    return bool(inspect(obj).attrs[attribute].history.has_changes())


def _config_text(session, obj) -> str:
    """The text a pending write would PERSIST for the reference-carrying attributes of `obj` (only those that
    actually change in this flush)."""
    parts = []
    for attribute in REFERENCE_ATTRIBUTES.get(type(obj), ()):
        if not _changed(session, obj, attribute):
            continue
        value = getattr(obj, attribute, None)
        if value is None:
            continue
        parts.append(value if isinstance(value, str) else json.dumps(value, default=str))
    return "\n".join(parts)


def assert_no_scheduled_asset_reference(session, config_objects) -> None:
    """The WRITER side of the reference/physical-deletion protocol (`ASSET_PROTOCOL`)."""
    if session.info.get(EXECUTOR_KEY) or not config_objects:
        return
    bodies = [(obj, _config_text(session, obj)) for obj in config_objects]
    bodies = [(obj, body) for obj, body in bodies if body]
    if not bodies:
        return
    if _is_postgres(session) and not session.info.get("_laboratory_cleanup_assets_locked"):
        session.execute(text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:k, 0))"), {"k": ASSET_LOCK_KEY})
        session.info["_laboratory_cleanup_assets_locked"] = True
    for _obj, body in bodies:
        hit = session.execute(
            text("SELECT 1 FROM laboratory_cleanup_object WHERE status IN " + REFUSED_OBJECT_STATUSES +
                 " AND position(object_key in :body) > 0 LIMIT 1"),
            {"body": body},
        ).first()
        if hit is not None:
            raise HTTPException(409, {
                "code": ASSET_SCHEDULED_CODE,
                "message": "Un archivo referenciado está programado para eliminarse por una limpieza del laboratorio.",
            })


def _is_postgres(session) -> bool:
    bind = session.get_bind()
    return bind is not None and bind.dialect.name == "postgresql"


def _tenants_of(session, instances: Iterable) -> Set[str]:
    tenants: Set[str] = set()
    for obj in instances:
        model = type(obj)
        if model in PARENT_MODELS:
            table, attr = PARENT_MODELS[model]
            parent_id = getattr(obj, attr, None)
            if parent_id is None:
                continue
            row = session.execute(
                text(f"SELECT tenant_id FROM {table} WHERE id = :i"), {"i": str(parent_id)}
            ).first()
            if row is not None and row[0] is not None:
                tenants.add(str(row[0]))
            continue
        if isinstance(obj, TENANT_MODELS):
            tenant_id = getattr(obj, "tenant_id", None)
            if tenant_id is not None:
                tenants.add(str(tenant_id))
    return tenants


def tenants_in_maintenance(session, tenant_ids) -> set:
    """Of `tenant_ids`, those whose maintenance gate is up. For non-ORM workers (delivery claims, reconciliation):
    takes each tenant's SHARED lock (held to the end of the caller's transaction) in sorted order and THEN reads the
    gate, exactly like an ORM writer, so a cleanup barrier either waits for the caller or is observed by it."""
    blocked = set()
    for tenant_id in sorted(str(t) for t in tenant_ids):
        if _is_postgres(session):
            session.execute(
                text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:k, 0))"), {"k": lock_key(tenant_id)}
            )
        if gate_is_up(session, tenant_id):
            blocked.add(tenant_id)
    return blocked


def assert_not_in_maintenance(session, tenant_id) -> None:
    """Take the shared lock (once per transaction and tenant) and refuse when the gate is up."""
    if session.info.get(EXECUTOR_KEY):
        return
    checked: Set[str] = session.info.setdefault(_CHECKED_KEY, set())
    tenant_key = str(tenant_id)
    if tenant_key in checked:
        return
    if _is_postgres(session):
        session.execute(
            text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:k, 0))"), {"k": lock_key(tenant_key)}
        )
    if gate_is_up(session, tenant_key):
        logger.warning(
            "Write refused: laboratory in cleanup maintenance",
            extra={"event": "laboratory_cleanup.write_refused", "tenant_id": tenant_key},
        )
        raise HTTPException(423, {"code": IN_PROGRESS_CODE, "message": IN_PROGRESS_MESSAGE})
    checked.add(tenant_key)


@event.listens_for(_OrmSession, "before_flush")
def _guard_operational_writes(session, flush_context, instances) -> None:
    if session.info.get(EXECUTOR_KEY):
        return
    changed = list(session.new) + list(session.dirty) + list(session.deleted)
    if not changed:
        return
    config = [
        obj for obj in changed
        if isinstance(obj, CONFIG_MODELS) and (obj in session.new or session.is_modified(obj)) and obj not in session.deleted
    ]  # `_config_text` keeps only the writes that change a reference-carrying attribute
    if config:
        assert_no_scheduled_asset_reference(session, config)
    relevant = [obj for obj in changed if type(obj) in PARENT_MODELS or isinstance(obj, TENANT_MODELS)]
    if not relevant:
        return
    # `session.dirty` includes objects whose attributes were merely touched; only real changes matter.
    relevant = [
        obj for obj in relevant
        if obj in session.new or obj in session.deleted or session.is_modified(obj)
    ]
    for tenant_id in sorted(_tenants_of(session, relevant)):
        assert_not_in_maintenance(session, tenant_id)


@event.listens_for(_OrmSession, "after_transaction_end")
def _forget_checked(session, transaction) -> None:
    # The shared lock is released with the root transaction, so the memo must go with it.
    if transaction.parent is None:
        session.info.pop(_CHECKED_KEY, None)
        session.info.pop("_laboratory_cleanup_assets_locked", None)


def barrier(session, tenant_id, *, timeout_seconds: int) -> None:
    """Wait until no writer that began before the gate is still in flight. Own short transaction:
    the caller commits/rolls back. Raises `TimeoutError` (retryable, nothing deleted) when in-flight
    writers do not finish within `timeout_seconds`."""
    if not _is_postgres(session):
        return
    session.execute(text(f"SET LOCAL lock_timeout = '{int(timeout_seconds) * 1000}ms'"))
    try:
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": lock_key(tenant_id)}
        )
    except OperationalError as exc:  # lock_timeout
        session.rollback()
        raise TimeoutError("writer barrier timed out") from exc
