"""Laboratory-content cleanup: preview, confirmation and the durable executor
(Céluma 1.4, issue #39; ADR 0021).

Lifecycle of an operation (`laboratory_cleanup_operation`):

    QUEUED  --claim-->  RUNNING [QUIESCE -> DATABASE -> OBJECTS] --> COMPLETED
                           |  failure          |  stale preview / cancel
                           v                   v
                        FAILED (retry)      CANCELLED (gate lifted, nothing deleted)

* Raising the gate is committing the QUEUED row: from then on every ORM writer of the tenant is
  refused (`app/core/maintenance_guard.py`).
* QUIESCE waits out in-flight writers (barrier) and refuses to continue over an in-flight email
  delivery or a running reconciliation.
* DATABASE is ONE atomic transaction: it re-verifies the approved `scope_revision`, records the
  object-store outbox, deletes the graph and reconciles usage. A rollback leaves everything as it was.
* OBJECTS runs after the commit, from the durable outbox, one object at a time, idempotently. A
  database rollback cannot restore removed objects, which is why they are removed last and from a list.
* COMPLETED only when the database phase committed AND every outbox object is DELETED/ABSENT.

All executor sessions set `EXECUTOR_KEY` so the guard does not refuse their own writes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.config import settings
from app.core.maintenance_guard import EXECUTOR_KEY, barrier, gate_is_up, lock_assets_exclusive
from app.models.audit import AuditLog
from app.models.laboratory_cleanup import (
    OBJECT_ABSENT,
    OBJECT_KEPT,
    OBJECT_DELETED,
    OBJECT_DONE_STATUSES,
    OBJECT_FAILED,
    OBJECT_PENDING,
    OPEN_STATUSES,
    PHASE_DATABASE,
    PHASE_DONE,
    PHASE_OBJECTS,
    PHASE_QUIESCE,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    LaboratoryCleanupObject,
    LaboratoryCleanupOperation,
)
from app.models.tenant import Tenant
from app.models.user import AppUser
from app.services import laboratory_cleanup_scope as scope_mod
from app.services.laboratory_cleanup_authority import CleanupOperator
from app.services.password_confirmation import require_actor_password
from app.services.s3 import S3Service
from app.services.storage_billing import StorageBillingService
from app.services.usage import UsageService
from app.services.usage_thresholds import UsageThresholdService

logger = logging.getLogger(__name__)

AUDIT_ENTITY = "laboratory_cleanup"
ATTEMPTED_ACTION = "LABORATORY_CLEANUP.CONFIRM"

MIN_REASON, MAX_REASON = 10, 1000


class CleanupError(Exception):
    """An expected, classified failure. `message` is safe to store and show (no keys, no SQL, no PHI)."""

    def __init__(self, code: str, message: str, *, retryable: bool = True):
        super().__init__(code)
        self.code = code
        self.message = message
        self.retryable = retryable


def _api_error(status: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message, **extra})


# --------------------------------------------------------------------------- preview token


def _key() -> bytes:
    # Derived with a purpose label: never the JWT signing key itself.
    return hmac.new(settings.jwt_secret.encode("utf-8"), b"laboratory-cleanup-preview-v1", hashlib.sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text_value: str) -> bytes:
    return base64.urlsafe_b64decode(text_value + "=" * (-len(text_value) % 4))


def issue_token(*, tenant_id, actor_id, revision: str, now: Optional[datetime] = None) -> tuple[str, str, datetime]:
    now = now or datetime.utcnow()
    expires = now + timedelta(seconds=settings.laboratory_cleanup_preview_ttl_seconds)
    jti = uuid.uuid4().hex
    payload = {
        "t": str(tenant_id), "a": str(actor_id), "r": revision,
        "j": jti, "e": int(expires.timestamp()),
    }
    body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    sig = _b64(hmac.new(_key(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}", jti, expires


def verify_token(token: str, *, tenant_id, actor_id, now: Optional[datetime] = None) -> dict:
    """Signature, expiry, tenant, actor and options — all from the SERVER-side claims. Every failure is
    the same safe 409 for the caller; the reason is only logged."""
    now = now or datetime.utcnow()
    def reject(reason: str):
        logger.warning("Cleanup confirmation rejected", extra={"event": "laboratory_cleanup.token_rejected", "reason": reason})
        raise _api_error(409, "preview_stale", "La vista previa ya no es válida. Genera una nueva vista previa.")
    try:
        body, sig = token.split(".", 1)
        expected = _b64(hmac.new(_key(), body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            reject("signature")
        claims = json.loads(_unb64(body))
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — malformed token
        reject("malformed")
    if int(claims["e"]) < int(now.timestamp()):
        reject("expired")
    if claims["t"] != str(tenant_id):
        reject("tenant")
    if claims["a"] != str(actor_id):
        reject("actor")
    return claims


def _fingerprint(*, tenant_id, actor_id, revision: str) -> str:
    return hashlib.sha256(f"{tenant_id}|{actor_id}|{revision}".encode()).hexdigest()


# --------------------------------------------------------------------------- audit


def _audit(session: Session, *, tenant_id, actor_id, action: str, entity_id, metadata: Dict[str, Any], description: str) -> None:
    session.add(AuditLog(
        tenant_id=tenant_id, actor_user_id=actor_id, created_by=actor_id, action=action,
        entity_type=AUDIT_ENTITY, entity_id=entity_id, description=description, event_metadata=metadata,
    ))


# --------------------------------------------------------------------------- preview


def preview(session: Session, operator: CleanupOperator) -> dict:
    """Read-only. Writes no domain row, allocates no number, moves no counter. The one write is a
    credential-free audit row in its own session."""
    scope = scope_mod.compute_scope(session, operator.tenant.id)
    session.rollback()  # end the read transaction; nothing was written
    laboratory = {"id": str(operator.tenant.id), "name": operator.tenant.name}
    data = scope.public(laboratory=laboratory)
    token = None
    expires = None
    if not scope.blockers:
        token, _jti, expires = issue_token(
            tenant_id=operator.tenant.id, actor_id=operator.user.id,
            revision=scope.revision,
        )
    data["preview_token"] = token
    data["expires_at"] = expires.isoformat() if expires else None
    with Session(session.get_bind()) as audit_session:
        audit_session.info[EXECUTOR_KEY] = True
        _audit(
            audit_session, tenant_id=operator.tenant.id, actor_id=operator.user.id,
            action="LABORATORY_CLEANUP.PREVIEWED", entity_id=operator.tenant.id,
            description="Laboratory cleanup previewed",
            metadata={"counts": scope.approved_summary(),
                      "blockers": [b["code"] for b in scope.blockers]},
        )
        audit_session.commit()
    return data


# --------------------------------------------------------------------------- confirm / start


@dataclass
class StartResult:
    operation: LaboratoryCleanupOperation
    replayed: bool


def _normalize_name(value: str) -> str:
    return unicodedata.normalize("NFC", (value or "").strip())


def start(
    session: Session,
    operator: CleanupOperator,
    *,
    preview_token: str,
    reason: str,
    confirm_name: str,
    password: Optional[str],
) -> StartResult:
    """Validate every confirmation element server-side, then commit the QUEUED row (the gate).
    Nothing is written before the last check passes except the throttled password-failure row."""
    user, tenant = operator.user, operator.tenant
    claims = verify_token(preview_token, tenant_id=tenant.id, actor_id=user.id)
    if _normalize_name(confirm_name) != _normalize_name(tenant.name):
        raise _api_error(422, "confirmation_name_mismatch", "El nombre escrito no coincide con el del laboratorio.")
    reason = (reason or "").strip()
    if not (MIN_REASON <= len(reason) <= MAX_REASON):
        raise _api_error(
            422, "reason_invalid", f"El motivo debe tener entre {MIN_REASON} y {MAX_REASON} caracteres."
        )
    require_actor_password(
        session, user=user, tenant_id=tenant.id, entity_type=AUDIT_ENTITY, entity_id=tenant.id,
        attempted_action=ATTEMPTED_ACTION, password=password,
    )

    fingerprint = _fingerprint(
        tenant_id=tenant.id, actor_id=user.id, revision=claims["r"]
    )
    def replay() -> Optional[StartResult]:
        existing = session.exec(
            select(LaboratoryCleanupOperation).where(LaboratoryCleanupOperation.confirmation_jti == claims["j"])
        ).first()
        if existing is None:
            return None
        if existing.confirmation_fingerprint == fingerprint and existing.tenant_id == tenant.id:
            return StartResult(existing, replayed=True)
        raise _api_error(409, "confirmation_already_used", "Esta confirmación ya fue utilizada.")

    if (found := replay()) is not None:
        return found

    # The authoritative freshness check: content hashes, not counts.
    scope = scope_mod.compute_scope(session, tenant.id)
    if scope.revision != claims["r"] or scope.blockers:
        session.rollback()
        # A concurrent duplicate of THIS confirmation may have just committed (and even started deleting): it is
        # a replay, not a stale preview. Look again before refusing.
        if (found := replay()) is not None:
            return found
        if scope.revision != claims["r"]:
            raise _api_error(409, "preview_stale", "El contenido del laboratorio cambió desde la vista previa. Genera una nueva.")
        raise _api_error(409, "cleanup_blocked", "Hay condiciones que impiden la limpieza.", blockers=scope.blockers)

    now = datetime.utcnow()
    op = LaboratoryCleanupOperation(
        tenant_id=tenant.id, actor_user_id=user.id, status=STATUS_QUEUED, phase=PHASE_QUIESCE,
        reason=reason, laboratory_name=tenant.name,
        scope_revision=scope.revision, confirmation_jti=claims["j"], confirmation_fingerprint=fingerprint,
        approved_counts=scope.approved_summary(), created_at=now, updated_at=now,
    )
    session.add(op)
    try:
        # The unique confirmation id / open-operation index can fire at the flush OR at the commit.
        session.flush()
        _audit(
            session, tenant_id=tenant.id, actor_id=user.id, action="LABORATORY_CLEANUP.CONFIRMED",
            entity_id=op.id, description="Laboratory cleanup confirmed",
            metadata={"operation_id": str(op.id), "reason": reason,
                      "counts": op.approved_counts},
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        # Either a concurrent duplicate of THIS confirmation (same jti) or another open operation.
        racer = session.exec(
            select(LaboratoryCleanupOperation).where(LaboratoryCleanupOperation.confirmation_jti == claims["j"])
        ).first()
        if racer is not None and racer.confirmation_fingerprint == fingerprint:
            return StartResult(racer, replayed=True)
        raise _api_error(409, "operation_open", "Ya existe una limpieza abierta para este laboratorio.")
    session.refresh(op)
    return StartResult(op, replayed=False)


# --------------------------------------------------------------------------- state helpers


def _executor_session(bind) -> Session:
    session = Session(bind)
    session.info[EXECUTOR_KEY] = True
    return session


def new_owner() -> str:
    return f"{os.getpid()}-{uuid.uuid4().hex[:10]}"


def _claim(bind, operation_id, owner: str) -> Optional[LaboratoryCleanupOperation]:
    """Atomically take the lease. None when the operation is terminal or another worker's lease is live."""
    now = datetime.utcnow()
    expires = now + timedelta(seconds=settings.laboratory_cleanup_lease_seconds)
    with _executor_session(bind) as session:
        row = session.execute(text("""
            UPDATE laboratory_cleanup_operation
               SET status = 'RUNNING', lease_owner = :o, lease_expires_at = :e, attempts = attempts + 1,
                   started_at = COALESCE(started_at, :now), updated_at = :now
             WHERE id = :i AND status IN ('QUEUED','FAILED','RUNNING')
               AND (lease_expires_at IS NULL OR lease_expires_at < :now OR lease_owner = :o)
            RETURNING id
        """), {"o": owner, "e": expires, "now": now, "i": str(operation_id)}).first()
        session.commit()
        if row is None:
            return None
        op = session.get(LaboratoryCleanupOperation, operation_id)
        session.refresh(op)
        session.expunge(op)
        return op


class LeaseLost(Exception):
    """This worker no longer owns the operation: it was cancelled, taken over by another worker, or its lease
    expired and nobody renewed it. The worker must stop WITHOUT touching state: whoever owns the operation now
    (or the cancellation) is authoritative, and reporting failure or completion from here would be a lie."""


def _fence(session: Session, operation_id, owner: str) -> None:
    """Execution fencing. Verify, and ROW-LOCK until this transaction ends, that `owner` still holds a live lease
    on a RUNNING operation. Every destructive or state-changing step of an executor calls this first, in the same
    transaction as the step, so a cancellation or a takeover (both single UPDATEs on this row) either committed
    before the check (the step is refused) or waits for the step's commit (and then finds the database phase
    done, which refuses cancellation). The owner token is unique per claim, so a paused old process can never
    be mistaken for its replacement."""
    row = session.execute(text(
        "SELECT 1 FROM laboratory_cleanup_operation WHERE id = :i AND status = 'RUNNING' AND lease_owner = :o "
        "AND lease_expires_at > :now FOR UPDATE"
    ), {"i": str(operation_id), "o": owner, "now": datetime.utcnow()}).first()
    if row is None:
        raise LeaseLost()


def _renew(bind, operation_id, owner: str) -> bool:
    """Extend the lease of the CURRENT owner. A row locked by a running database phase is skipped (that
    transaction is itself the proof of liveness). False only when ownership is genuinely gone."""
    expires = datetime.utcnow() + timedelta(seconds=settings.laboratory_cleanup_lease_seconds)
    with _executor_session(bind) as session:
        row = session.execute(text("""
            UPDATE laboratory_cleanup_operation SET lease_expires_at = :e, updated_at = :now
             WHERE id = (SELECT id FROM laboratory_cleanup_operation
                          WHERE id = :i AND status = 'RUNNING' AND lease_owner = :o FOR UPDATE SKIP LOCKED)
            RETURNING id
        """), {"e": expires, "now": datetime.utcnow(), "i": str(operation_id), "o": owner}).first()
        session.commit()
        if row is not None:
            return True
        still_mine = session.execute(text(
            "SELECT 1 FROM laboratory_cleanup_operation WHERE id = :i AND status = 'RUNNING' AND lease_owner = :o"
        ), {"i": str(operation_id), "o": owner}).first()
        return still_mine is not None


class _Heartbeat:
    """Renews the lease while a long phase runs, so a slow-but-alive worker is never mistaken for a dead one.
    It only ever extends ITS OWN lease; if ownership is gone it records that and stops."""

    def __init__(self, bind, operation_id, owner: str):
        self.bind, self.operation_id, self.owner = bind, operation_id, owner
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="laboratory-cleanup-heartbeat", daemon=True)

    def _run(self) -> None:
        interval = max(settings.laboratory_cleanup_lease_seconds / 3.0, 0.05)
        while not self._stop.wait(interval):
            try:
                if not _renew(self.bind, self.operation_id, self.owner):
                    self.lost.set()
                    return
            except Exception:  # noqa: BLE001 — a transient failure is retried at the next beat
                logger.warning("Laboratory cleanup heartbeat failed", extra={"event": "laboratory_cleanup.heartbeat_failed"})

    def check(self) -> None:
        if self.lost.is_set():
            raise LeaseLost()

    def __enter__(self) -> "_Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def _set_phase(bind, operation_id, owner: str, phase: str) -> None:
    with _executor_session(bind) as session:
        row = session.execute(text(
            "UPDATE laboratory_cleanup_operation SET phase = :p, updated_at = :now "
            "WHERE id = :i AND status = 'RUNNING' AND lease_owner = :o RETURNING id"
        ), {"p": phase, "now": datetime.utcnow(), "i": str(operation_id), "o": owner}).first()
        session.commit()
        if row is None:
            raise LeaseLost()


def _fail(bind, operation_id, tenant_id, actor_id, error: CleanupError, owner: str) -> None:
    with _executor_session(bind) as session:
        row = session.execute(text("""
            UPDATE laboratory_cleanup_operation
               SET status = 'FAILED', error_code = :c, error_message = :m, lease_owner = NULL,
                   lease_expires_at = NULL, updated_at = :now
             WHERE id = :i AND status = 'RUNNING' AND lease_owner = :o
            RETURNING id
        """), {"c": error.code, "m": error.message[:500], "now": datetime.utcnow(), "i": str(operation_id), "o": owner}).first()
        if row is None:
            session.rollback()  # superseded or cancelled meanwhile: not ours to report
            return
        action = "LABORATORY_CLEANUP.OBJECTS_FAILED" if error.code == "objects_failed" else "LABORATORY_CLEANUP.FAILED"
        _audit(session, tenant_id=tenant_id, actor_id=actor_id, action=action, entity_id=operation_id,
               description="Laboratory cleanup failed", metadata={"operation_id": str(operation_id), "error_code": error.code})
        session.commit()
    logger.error(
        "Laboratory cleanup failed",
        extra={"event": "laboratory_cleanup.failed", "operation_id": str(operation_id),
               "tenant_id": str(tenant_id), "error_code": error.code},
    )


def _cancel_row(bind, operation_id, tenant_id, actor_id, *, code: Optional[str], message: Optional[str],
                owner: Optional[str] = None) -> bool:
    """Cancel in ONE conditional UPDATE. `owner` given: the executor cancelling its own stale preview (it must
    still own the operation). `owner` None: an operator's cancellation, allowed only while NO worker holds a live
    lease. Either way it cannot succeed once the database phase committed, and a running database phase holds
    this row's lock, so the cancellation waits for it and is then refused."""
    with _executor_session(bind) as session:
        row = session.execute(text("""
            UPDATE laboratory_cleanup_operation
               SET status = 'CANCELLED', cancelled_at = :now, updated_at = :now, lease_owner = NULL,
                   lease_expires_at = NULL, error_code = :c, error_message = :m
             WHERE id = :i AND status IN ('QUEUED','RUNNING','FAILED') AND database_cleaned_at IS NULL
               AND ( (CAST(:o AS text) IS NOT NULL AND status = 'RUNNING' AND lease_owner = CAST(:o AS text))
                  OR (CAST(:o AS text) IS NULL AND (lease_expires_at IS NULL OR lease_expires_at < :now)) )
            RETURNING id
        """), {"now": datetime.utcnow(), "c": code, "m": message, "i": str(operation_id), "o": owner}).first()
        if row is not None:
            _audit(session, tenant_id=tenant_id, actor_id=actor_id, action="LABORATORY_CLEANUP.CANCELLED",
                   entity_id=operation_id, description="Laboratory cleanup cancelled",
                   metadata={"operation_id": str(operation_id), "reason_code": code})
        session.commit()
        return row is not None


# --------------------------------------------------------------------------- phases


def _phase_quiesce(bind, op: LaboratoryCleanupOperation, owner: Optional[str] = None) -> None:
    owner = owner or op.lease_owner
    with _executor_session(bind) as session:
        try:
            barrier(session, op.tenant_id, timeout_seconds=settings.laboratory_cleanup_barrier_timeout_seconds)
            session.commit()
        except TimeoutError:
            session.rollback()
            raise CleanupError("barrier_timeout", "Hay operaciones en curso en el laboratorio; reintenta en un momento.")
    _set_phase(bind, op.id, owner, PHASE_DATABASE)


def _phase_database(bind, op: LaboratoryCleanupOperation, owner: str) -> Optional[bool]:
    """One atomic transaction. Returns True when it committed, False when the operation was cancelled as stale and
    None when this worker no longer owns the operation (nothing was touched).

    The first statement FENCES the worker (live lease + owner + RUNNING, row-locked until commit), so a worker that
    was cancelled, superseded or whose lease expired can never reach the deletions. The second takes the global
    asset lock EXCLUSIVELY: preserved-configuration writers (templates, letterheads — any laboratory) hold it
    shared while they write, so every reference committed before this point is visible to the object resolution
    below, and every writer that comes after waits for our commit and then finds its key scheduled for deletion."""
    with _executor_session(bind) as session:
        try:
            _fence(session, op.id, owner)
            lock_assets_exclusive(session)
            scope = scope_mod.compute_scope(session, op.tenant_id)
            if scope.revision != op.scope_revision:
                session.rollback()
                _cancel_row(bind, op.id, op.tenant_id, op.actor_user_id, code="preview_stale",
                            message="El contenido cambió antes de iniciar; la limpieza se canceló sin eliminar nada.",
                            owner=owner)
                return False
            blockers = [b for b in scope.blockers if b["code"] != "operation_open"]
            if blockers:
                retryable = {b["code"] for b in blockers} <= {"deliveries_in_flight", "reconciliation_running"}
                session.rollback()
                raise CleanupError(blockers[0]["code"], blockers[0]["message"], retryable=retryable)

            now = datetime.utcnow()
            # The object outbox is written in THIS transaction (rollback => no outbox, nothing removed).
            for ref in scope.objects.physical:
                session.add(LaboratoryCleanupObject(
                    operation_id=op.id, tenant_id=op.tenant_id, storage_object_id=uuid.UUID(ref.storage_object_id),
                    provider=ref.provider, bucket=ref.bucket, object_key=ref.object_key,
                    size_bytes=ref.size_bytes, category=ref.category, status=OBJECT_PENDING, created_at=now,
                ))
            session.flush()
            done = scope_mod.delete_graph(session, scope, operation_id=op.id, now=now)
            row_ids = [ref.storage_object_id for ref in scope.objects.delete]
            if row_ids:
                result = session.execute(
                    text("DELETE FROM storage_object WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": row_ids}
                )
                done["storage_object_rows"] = int(result.rowcount or 0)

            # Usage: recompute from what REMAINS (logos, signatures, templates, letterheads), never zero.
            usage = UsageService.get_usage(session, op.tenant_id)
            if usage is not None:
                actual = StorageBillingService.compute_billable_storage_bytes(session, op.tenant_id)
                delta = actual - usage.billable_storage_bytes
                if delta != 0:
                    UsageService.adjust_storage(session, op.tenant_id, delta, source="laboratory_cleanup",
                                                resource_type="laboratory_cleanup")
                done["billable_storage_bytes_after"] = actual
                UsageThresholdService.evaluate_storage(session, op.tenant_id, source="laboratory_cleanup")

            executed = {"deleted": {k: v for k, v in done.items() if not k.startswith("retired") and k != "order_pointers_cleared"},
                        "retired_codes": done.get("retired_codes_orders", 0) + done.get("retired_codes_samples", 0),
                        "objects_queued": len(scope.objects.physical)}
            session.execute(text("""
                UPDATE laboratory_cleanup_operation
                   SET phase = 'OBJECTS', database_cleaned_at = :now, executed_counts = CAST(:ec AS json), updated_at = :now,
                       lease_expires_at = :lease
                 WHERE id = :i
            """), {"now": now, "ec": json.dumps(executed), "i": str(op.id),
                   # a long transaction can outlive the lease (the heartbeat cannot write the row we hold locked):
                   # renew it in the same statement so the owner is still the owner the moment it commits.
                   "lease": datetime.utcnow() + timedelta(seconds=settings.laboratory_cleanup_lease_seconds)})
            _audit(session, tenant_id=op.tenant_id, actor_id=op.actor_user_id,
                   action="LABORATORY_CLEANUP.DATABASE_COMPLETED", entity_id=op.id,
                   description="Laboratory cleanup database phase completed",
                   metadata={"operation_id": str(op.id), "counts": executed})
            session.commit()
        except LeaseLost:
            session.rollback()
            return None
        except (CleanupError, HTTPException):
            session.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 — the transaction rolls back; nothing was deleted
            session.rollback()
            logger.error(
                "Laboratory cleanup database phase failed",
                extra={"event": "laboratory_cleanup.database_failed", "operation_id": str(op.id),
                       "tenant_id": str(op.tenant_id), "exception_type": type(exc).__name__},
            )
            raise CleanupError("database_error", "La fase de base de datos falló y se revirtió sin cambios. Puedes reintentar.")
    return True


def _delete_one_object(session: Session, s3, row: LaboratoryCleanupObject) -> str:
    """Delete one outbox object and VERIFY it is gone. Returns the final status.

    Last line of defence against a reference created after the outbox was written by a path the writer guard does
    not see: if ANY preserved configuration text or ANY `storage_object` row still names this bucket/key, the
    object is KEPT (never deleted) and the outbox row is closed as such."""
    if row.bucket != s3.bucket:
        # Never delete in a bucket this process is not configured for.
        raise CleanupError("bucket_mismatch", "El objeto pertenece a otro bucket.", retryable=False)
    # Reference/deletion protocol (`maintenance_guard.ASSET_PROTOCOL`): the global asset lock is taken EXCLUSIVELY
    # and held by this transaction across the re-check AND the object-store delete + verification. A writer that
    # committed a reference earlier is seen below (KEPT); one that arrives later waits here and is then refused,
    # because this row is still PENDING/FAILED (and DELETED/ABSENT afterwards). The caller ends the transaction
    # (releasing the lock) right after this returns; the operation row is NOT locked meanwhile.
    try:
        lock_assets_exclusive(session, settings.laboratory_cleanup_barrier_timeout_seconds)
    except TimeoutError:
        raise CleanupError("asset_lock_timeout", "Hay escrituras de configuración en curso; se reintentará.")
    if scope_mod.object_is_still_referenced(session, row.tenant_id, row.bucket, row.object_key):
        return OBJECT_KEPT
    if s3.head_object(row.object_key) is None:
        return OBJECT_ABSENT
    s3.delete_object(row.object_key)
    if s3.head_object(row.object_key) is not None:
        raise CleanupError("still_present", "El objeto sigue presente tras eliminarlo.")
    return OBJECT_DELETED


def _phase_objects(bind, op: LaboratoryCleanupOperation, owner: str) -> None:
    s3 = S3Service()
    failed = 0
    with _executor_session(bind) as session:
        _fence(session, op.id, owner)
        pending_ids = [r for (r,) in session.execute(text(
            "SELECT id FROM laboratory_cleanup_object WHERE operation_id = :o AND status NOT IN ('DELETED','ABSENT','KEPT') "
            "ORDER BY created_at, id"
        ), {"o": str(op.id)}).all()]
        session.rollback()
    for object_id in pending_ids:
        # 1) fence + snapshot the outbox row, then RELEASE the row lock: no lock is held across network calls.
        with _executor_session(bind) as session:
            _fence(session, op.id, owner)  # a cancelled/superseded worker stops before touching the next object
            row = session.get(LaboratoryCleanupObject, object_id)
            session.expunge(row)
            session.rollback()
        # 2) the object-store work (and its last-line reference check), outside any fenced transaction.
        status, error_code = OBJECT_FAILED, None
        with _executor_session(bind) as session:
            try:
                status, error_code = _delete_one_object(session, s3, row), None
            except CleanupError as exc:
                status, error_code = OBJECT_FAILED, exc.code
            except Exception as exc:  # noqa: BLE001 — the SDK error text may quote bucket/key: store only a class code
                status, error_code = OBJECT_FAILED, "object_store_error"
                logger.error(
                    "Laboratory cleanup object deletion failed",
                    extra={"event": "laboratory_cleanup.object_failed", "operation_id": str(op.id),
                           "exception_type": type(exc).__name__},
                )
            session.rollback()
        if status == OBJECT_FAILED:
            failed += 1
        # 3) record the outcome ONLY if this worker still owns the operation (never overwrite the current owner's outbox).
        with _executor_session(bind) as session:
            _fence(session, op.id, owner)
            current = session.get(LaboratoryCleanupObject, object_id)
            current.attempts += 1
            current.status, current.error_code = status, error_code
            current.deleted_at = datetime.utcnow() if status != OBJECT_FAILED else None
            session.add(current)
            session.commit()
    if failed:
        raise CleanupError("objects_failed", f"No se pudieron eliminar {failed} objeto(s) del almacenamiento. Puedes reintentar.")


def _complete(bind, op: LaboratoryCleanupOperation, owner: str) -> None:
    with _executor_session(bind) as session:
        _fence(session, op.id, owner)
        remaining = session.execute(text(
            "SELECT count(*) FROM laboratory_cleanup_object WHERE operation_id = :o AND status NOT IN ('DELETED','ABSENT','KEPT')"
        ), {"o": str(op.id)}).scalar()
        if remaining:
            raise CleanupError("objects_failed", "Quedan objetos pendientes de eliminar.")
        now = datetime.utcnow()
        session.execute(text("""
            UPDATE laboratory_cleanup_operation
               SET status = 'COMPLETED', phase = 'DONE', completed_at = :now, updated_at = :now,
                   lease_owner = NULL, lease_expires_at = NULL, error_code = NULL, error_message = NULL
             WHERE id = :i AND lease_owner = :o AND status = 'RUNNING'
        """), {"now": now, "i": str(op.id), "o": owner})
        _audit(session, tenant_id=op.tenant_id, actor_id=op.actor_user_id, action="LABORATORY_CLEANUP.COMPLETED",
               entity_id=op.id, description="Laboratory cleanup completed", metadata={"operation_id": str(op.id)})
        session.commit()


# --------------------------------------------------------------------------- the executor


def run_operation(bind, operation_id, *, owner: Optional[str] = None) -> Optional[str]:
    """Drive the operation as far as it can go. Idempotent and safe to call from any number of workers:
    only the lease holder proceeds, and every step is fenced on that ownership (see `_fence`). Returns the final
    status seen by this call, or None when it did not (or no longer) owns the operation."""
    owner = owner or new_owner()
    op = _claim(bind, operation_id, owner)
    if op is None:
        return None
    started_phase = op.phase
    try:
        with _Heartbeat(bind, op.id, owner) as beat:
            if op.phase == PHASE_QUIESCE:
                with _executor_session(bind) as session:
                    _fence(session, op.id, owner)
                    _audit(session, tenant_id=op.tenant_id, actor_id=op.actor_user_id, action="LABORATORY_CLEANUP.STARTED",
                           entity_id=op.id, description="Laboratory cleanup started",
                           metadata={"operation_id": str(op.id), "attempt": op.attempts})
                    session.commit()
                _phase_quiesce(bind, op, owner)
                beat.check()
                op.phase = PHASE_DATABASE
            if op.phase == PHASE_DATABASE:
                outcome = _phase_database(bind, op, owner)
                if outcome is None:
                    raise LeaseLost()
                if outcome is False:
                    return STATUS_CANCELLED
                beat.check()
                op.phase = PHASE_OBJECTS
            if op.phase == PHASE_OBJECTS:
                _phase_objects(bind, op, owner)
                beat.check()
                _complete(bind, op, owner)
                return STATUS_COMPLETED
            return None
    except LeaseLost:
        logger.warning(
            "Laboratory cleanup worker lost ownership and stopped",
            extra={"event": "laboratory_cleanup.lease_lost", "operation_id": str(op.id), "phase": started_phase},
        )
        return None
    except CleanupError as error:
        _fail(bind, op.id, op.tenant_id, op.actor_user_id, error, owner)
        return STATUS_FAILED
    except HTTPException as exc:  # a guard/permission refusal inside the executor is a bug: fail loudly, retryable
        _fail(bind, op.id, op.tenant_id, op.actor_user_id, CleanupError("executor_refused", f"HTTP {exc.status_code}"), owner)
        return STATUS_FAILED
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Laboratory cleanup crashed",
            extra={"event": "laboratory_cleanup.crashed", "operation_id": str(op.id),
                   "phase": started_phase, "exception_type": type(exc).__name__},
        )
        _fail(bind, op.id, op.tenant_id, op.actor_user_id, CleanupError("unexpected_error", "Error inesperado; puedes reintentar."), owner)
        return STATUS_FAILED


# --------------------------------------------------------------------------- retry / cancel / read


def get_operation(session: Session, tenant_id, operation_id) -> LaboratoryCleanupOperation:
    try:
        op_uuid = uuid.UUID(str(operation_id))
    except ValueError:
        raise HTTPException(404, "Operation not found") from None
    op = session.get(LaboratoryCleanupOperation, op_uuid)
    if op is None or str(op.tenant_id) != str(tenant_id):
        raise HTTPException(404, "Operation not found")
    return op


def can_retry(op: LaboratoryCleanupOperation, now: Optional[datetime] = None) -> bool:
    now = now or datetime.utcnow()
    if op.status == STATUS_FAILED:
        return True
    if op.status in (STATUS_QUEUED, STATUS_RUNNING):
        return op.lease_expires_at is None or op.lease_expires_at < now
    return False


def can_cancel(op: LaboratoryCleanupOperation, now: Optional[datetime] = None) -> bool:
    now = now or datetime.utcnow()
    if op.database_cleaned_at is not None or op.status not in OPEN_STATUSES:
        return False
    return op.lease_expires_at is None or op.lease_expires_at < now


def request_cancel(session: Session, operator: CleanupOperator, op: LaboratoryCleanupOperation) -> LaboratoryCleanupOperation:
    if op.status == STATUS_CANCELLED:
        return op
    if not can_cancel(op):
        raise _api_error(
            409, "cancel_not_allowed",
            "La limpieza ya modificó la base de datos o sigue en ejecución; no se puede cancelar.",
        )
    done = _cancel_row(session.get_bind(), op.id, op.tenant_id, operator.user.id, code="cancelled_by_operator",
                       message="Cancelada por el operador antes de modificar datos.")
    session.expire_all()
    current = session.get(LaboratoryCleanupOperation, op.id)
    if not done and current.status != STATUS_CANCELLED:
        # The conditional UPDATE lost a race (a worker took the lease, or the database phase committed).
        raise _api_error(
            409, "cancel_not_allowed",
            "La limpieza ya modificó la base de datos o sigue en ejecución; no se puede cancelar.",
        )
    return current


def object_summary(session: Session, operation_id) -> dict:
    rows = session.execute(text("""
        SELECT status, count(*), coalesce(sum(size_bytes), 0)
        FROM laboratory_cleanup_object WHERE operation_id = :o GROUP BY status
    """), {"o": str(operation_id)}).all()
    summary = {"total": 0, "pending": 0, "deleted": 0, "absent": 0, "kept": 0, "failed": 0, "pending_bytes": 0}
    for status, count, size in rows:
        summary["total"] += int(count)
        key = status.lower()
        summary[key] = int(count)
        if status in (OBJECT_PENDING, OBJECT_FAILED):
            summary["pending_bytes"] += int(size)
    return summary


def view(session: Session, op: LaboratoryCleanupOperation) -> dict:
    now = datetime.utcnow()
    return {
        "id": str(op.id),
        "tenant_id": str(op.tenant_id),
        "status": op.status,
        "phase": op.phase,
        "reason": op.reason,
        "laboratory_name": op.laboratory_name,
        "actor_user_id": str(op.actor_user_id),
        "approved_counts": op.approved_counts,
        "executed_counts": op.executed_counts,
        "error_code": op.error_code,
        "error_message": op.error_message,
        "attempts": op.attempts,
        "objects": object_summary(session, op.id),
        "writers_blocked": op.status in OPEN_STATUSES and op.database_cleaned_at is None,
        "can_retry": can_retry(op, now),
        "can_cancel": can_cancel(op, now),
        "created_at": op.created_at.isoformat() if op.created_at else None,
        "started_at": op.started_at.isoformat() if op.started_at else None,
        "database_cleaned_at": op.database_cleaned_at.isoformat() if op.database_cleaned_at else None,
        "completed_at": op.completed_at.isoformat() if op.completed_at else None,
        "cancelled_at": op.cancelled_at.isoformat() if op.cancelled_at else None,
    }


def gate_is_open_for(session: Session, tenant_id) -> bool:
    """Used by reconciliation and the delivery worker."""
    return gate_is_up(session, tenant_id)
