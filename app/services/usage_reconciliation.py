"""UsageReconciliationService — the reconciliation engine (Céluma 1.3,
Phase 4, Block D).

Two concerns, deliberately kept separate (see docs/celuma-1.3/
phase-4-block-d/reconciliation-domain-contract.md):

1. **Accounting reconciliation** — `TenantUsage.billable_storage_bytes`
   (the incrementally-maintained commercial counter) against
   `StorageBillingService.compute_billable_storage_bytes()` (the
   authoritative DB recomputation). This is the only part allowed to
   *mutate* anything: it may repair the counter, and nothing else.

2. **Storage integrity verification** — DB `StorageObject` metadata
   against actual S3 object state (`head_object`), plus a listing sweep
   of the tenant-attributable key prefixes for physical objects no
   `StorageObject` row tracks. Strictly READ ONLY: it deletes nothing,
   rewrites nothing, and never feeds its findings back into the billable
   counter.

A physical S3 orphan is not automatically billable, and a missing S3
object is not permission to delete DB metadata — in a clinical system a
missing artifact is an incident to investigate, not a row to clean up.
Block D reports both and repairs neither.

Transaction shape (one reconciliation run)
-------------------------------------------
    stale-run recovery                       -- its own transaction
    INSERT tenant_usage_reconciliation RUNNING; COMMIT

    BEGIN                                    -- the accounting snapshot
      SELECT tenant_usage ... FOR UPDATE
      actual = StorageBillingService.compute_billable_storage_bytes()
      snapshot billable objects + tracked keys + logo integrity
      optional repair
    COMMIT

    S3 HEAD/LIST verification                -- NO transaction open

    UPDATE the run row to its terminal status; COMMIT

Why the row lock makes the accounting comparison race-safe: every normal
application storage write mutates the `StorageObject` metadata and the
`TenantUsage` counter in the *same* transaction (Block C's incremental
accounting contract). Locking the `TenantUsage` row therefore blocks such
a writer from committing its half-applied state during the snapshot, and
any `StorageObject` it has already inserted but not committed is
invisible to this transaction. Expected and actual are read from one
coherent point in time.

The S3 work is deliberately outside that transaction: an S3 round trip
inside a row lock would hold the tenant's counter against every concurrent
upload for the duration of a network call — the same reason
`NotificationDeliveryWorker` splits its claim from its provider call.

Privacy: no log line here carries a patient name, a report title, an
object key, a presigned URL, a bucket name or a raw AWS exception. What
they carry is ids, counts, byte totals, a category label and a sanitized
error code.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence, Set
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.config import settings
from app.models.storage import StorageObject
from app.models.tenant import Tenant
from app.models.tenant_usage import TenantUsage
from app.models.tenant_usage_reconciliation import (
    TenantUsageReconciliation,
    TenantUsageReconciliationStatus,
)
from app.services.s3 import S3Service
from app.services.storage_billing import (
    BillableStorageObjectRef,
    StorageBillingService,
    tenant_logo_key_prefix,
)
from app.services.usage import UsageService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sanitized failure codes (tenant_usage_reconciliation.error_code)
# ---------------------------------------------------------------------------
#
# Same content policy as `notification_delivery.error_code`: a stable code,
# never a raw exception message, S3 key, credential or patient-identifying
# detail.

ERROR_S3_ACCESS_DENIED = "s3_access_denied"
ERROR_S3_TIMEOUT = "s3_timeout"
ERROR_S3_UNAVAILABLE = "s3_unavailable"
ERROR_CONCURRENT_RECONCILIATION = "concurrent_reconciliation"
ERROR_STALE_RUN_RECOVERED = "stale_run_recovered"
ERROR_UNEXPECTED = "unexpected_error"

# There is deliberately no `usage_not_initialized` failure code. Block D's
# ratified policy for a missing `TenantUsage` row is *recovery*, not
# failure: the complete authoritative baseline can always be computed from
# DB relationships alone, so reconciliation initializes the row from that
# full calculation rather than refusing to run (or, worse, lazily seeding a
# partial value — the exact mistake `UsageService.adjust_storage`'s
# never-create rule exists to prevent). See
# accounting-reconciliation-contract.md §"Missing TenantUsage".


#: The S3 key prefixes whose second path segment is, by construction, a
#: tenant id — the only prefixes an orphan sweep can attribute to a tenant
#: without guessing. Verified against the live key layouts:
#:
#:   samples/{tenant_id}/{branch_id}/{sample_id}/...   (app/api/v1/laboratory.py)
#:   reports/{tenant_id}/...                           (app/api/v1/reports.py,
#:                                                      report_pdf_generation.py)
#:   tenants/{tenant_id}/logo/...                      (app/api/v1/tenants.py)
#:   users/{tenant_id}/{user_id}/signature/...         (app/api/v1/users.py)
#:
#: Deliberately excluded: `avatars/{user_id}/...` (no tenant segment),
#: `report-letterheads/{letterhead_id}/...` and
#: `report-templates/{template_id}/...` (keyed by entity, not tenant — the
#: tenant is only recoverable through a DB join, so a listing sweep cannot
#: attribute a *stray* object under them to any tenant). See
#: s3-integrity-reconciliation-contract.md §"Known attribution limitations".
TENANT_ATTRIBUTABLE_KEY_PREFIXES: tuple[str, ...] = (
    "samples/",
    "reports/",
    "tenants/",
    "users/",
)

#: Marks a retained-after-delete signature PNG (risk R6): the application
#: deliberately deletes the `StorageObject` row while leaving the S3 object
#: in place, so historical signed-report URLs keep resolving. Accepted,
#: by-design divergence — counted separately, never reported as an
#: actionable orphan.
SIGNATURE_KEY_MARKER = "/signature/"


class ConcurrentReconciliationError(RuntimeError):
    """Another reconciliation is already RUNNING for this tenant.

    Raised instead of silently starting a second run: the partial unique
    index `ix_tenant_usage_reconciliation_one_running` makes two concurrent
    RUNNING rows for one tenant unrepresentable, and this is that
    constraint surfacing as an application-level outcome.
    """

    def __init__(self, tenant_id: UUID):
        self.tenant_id = tenant_id
        self.error_code = ERROR_CONCURRENT_RECONCILIATION
        super().__init__(f"A reconciliation is already running for tenant {tenant_id}")


@dataclass(frozen=True)
class S3IntegrityFindings:
    """What the read-only S3 sweep found. Counts only — no key, no bucket."""

    objects_checked: int = 0
    missing_objects_found: int = 0
    metadata_mismatches_found: int = 0
    orphans_found: int = 0
    accepted_retained_objects: int = 0


@dataclass(frozen=True)
class ReconciliationOutcome:
    """One reconciliation run's result, as returned to the worker and the
    manual endpoint. Aggregates only: never an object key, never a bucket,
    never a raw AWS message."""

    reconciliation_id: UUID
    tenant_id: UUID
    status: str
    started_at: datetime
    completed_at: Optional[datetime]

    expected_storage_bytes: Optional[int]
    actual_storage_bytes: Optional[int]
    difference_bytes: Optional[int]
    repaired: bool

    objects_checked: Optional[int] = None
    orphans_found: Optional[int] = None
    missing_objects_found: Optional[int] = None
    metadata_mismatches_found: Optional[int] = None

    #: Retained-after-delete signature PNGs (R6). Accepted divergence,
    #: tracked for operators, deliberately NOT part of `orphans_found`.
    accepted_retained_objects: int = 0
    #: `Tenant.logo_storage_id` pointing at a StorageObject that belongs to
    #: another tenant or is not a tenant-logo object at all.
    logo_integrity_errors: int = 0
    #: `logo_url` set with no resolvable `logo_storage_id` — the legacy
    #: reference the Block D migration could not backfill.
    legacy_logo_unresolved: bool = False
    #: This run created the tenant's missing `TenantUsage` row from a
    #: complete authoritative baseline (see §"missing usage" below).
    usage_initialized: bool = False
    error_code: Optional[str] = None


@dataclass(frozen=True)
class _AccountingSnapshot:
    """Everything the S3 phase needs, copied out of the ORM before the
    accounting transaction ends.

    Frozen plain values on purpose — same discipline as the notification
    worker's `DeliverySendContext`: reading an attribute off a live ORM
    object after the commit would silently open a new transaction, and the
    whole point of this split is that no transaction is open across an S3
    round trip.
    """

    expected_bytes: Optional[int]
    actual_bytes: int
    difference_bytes: Optional[int]
    repaired: bool
    usage_initialized: bool
    billable_objects: tuple[BillableStorageObjectRef, ...]
    tracked_keys: frozenset[str]
    logo_integrity_errors: int
    legacy_logo_unresolved: bool


def _stale_threshold_seconds() -> int:
    return settings.usage_reconciliation_stale_seconds


def recover_stale_runs(
    session: Session,
    *,
    tenant_id: Optional[UUID] = None,
    stale_seconds: Optional[int] = None,
    now: Optional[datetime] = None,
) -> int:
    """Terminate RUNNING rows abandoned by a process that died mid-run.

    A worker (or an API process) can insert a RUNNING row and then be killed
    before it writes a terminal status. Nothing else would ever notice: the
    row stays RUNNING forever, and — because of the partial unique index —
    would block every future run for that tenant. This is the recovery the
    `(status, started_at)` index Block B shipped exists to serve.

    Returns the number of rows recovered. Its own transaction; safe to call
    at the start of every worker cycle and every reconciliation.
    """
    now = now or datetime.utcnow()
    threshold = now - timedelta(
        seconds=stale_seconds if stale_seconds is not None else _stale_threshold_seconds()
    )

    statement = select(TenantUsageReconciliation).where(
        TenantUsageReconciliation.status
        == TenantUsageReconciliationStatus.RUNNING.value,
        TenantUsageReconciliation.started_at < threshold,
    )
    if tenant_id is not None:
        statement = statement.where(TenantUsageReconciliation.tenant_id == tenant_id)

    recovered = 0
    for row in session.exec(statement).all():
        row.status = TenantUsageReconciliationStatus.FAILED
        row.completed_at = now
        row.error_code = ERROR_STALE_RUN_RECOVERED
        session.add(row)
        recovered += 1
        logger.warning(
            "Recovered a stale RUNNING reconciliation",
            extra={
                "event": "usage_reconciliation.stale_run_recovered",
                "tenant_id": str(row.tenant_id),
                "reconciliation_id": str(row.id),
                "error_code": ERROR_STALE_RUN_RECOVERED,
            },
        )
    if recovered:
        session.commit()
    else:
        # Nothing to write, but the SELECT above opened a transaction.
        session.rollback()
    return recovered


def _classify_s3_error(exc: BaseException) -> str:
    """Map an S3/botocore exception to a sanitized, stable error code.

    Classifies by exception *shape* (type name, error code) rather than by
    importing botocore's exception hierarchy, so nothing here depends on the
    SDK's internal class layout — and so `str(exc)`, which routinely quotes
    bucket names and request ids, never reaches a log line or a column.
    """
    name = type(exc).__name__
    code = ""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = str(response.get("Error", {}).get("Code", "") or "")

    if code in {"AccessDenied", "AllAccessDisabled", "403"} or "AccessDenied" in name:
        return ERROR_S3_ACCESS_DENIED
    if "Timeout" in name or "TimeoutError" in name or code in {"RequestTimeout"}:
        return ERROR_S3_TIMEOUT
    return ERROR_S3_UNAVAILABLE


class UsageReconciliationService:
    """Reconciles one tenant at a time. Stateless apart from its S3 client.

    The same instance is used by the in-process worker and by the manual
    admin endpoint — there is exactly one implementation of what a
    reconciliation *is*, and the two entry points differ only in what
    triggers them.
    """

    def __init__(self, s3: Optional[S3Service] = None):
        self._s3 = s3

    # -- S3 client ---------------------------------------------------------

    def _s3_client(self) -> S3Service:
        """Built lazily so a reconciliation with `verify_s3=False` never
        constructs a boto3 client (and never needs AWS configuration at
        all)."""
        if self._s3 is None:
            self._s3 = S3Service()
        return self._s3

    # -- public API --------------------------------------------------------

    def reconcile_tenant(
        self,
        session: Session,
        tenant_id: UUID,
        *,
        repair: bool = True,
        verify_s3: bool = True,
        now: Optional[datetime] = None,
    ) -> ReconciliationOutcome:
        """Run one full reconciliation for `tenant_id`.

        Never raises for an expected failure: an S3 outage or an unexpected
        error ends as a `FAILED` run with a sanitized `error_code`, returned
        rather than propagated — the worker must keep going to the next
        tenant, and the endpoint must be able to report what happened. (A
        missing `TenantUsage` row is not a failure at all: it is recovered
        from the complete authoritative baseline — see `_run_accounting`.)

        The one exception is `ConcurrentReconciliationError`, which is
        raised: "someone else is already doing this" is not a failed run and
        must not create a second history row.
        """
        started_at = now or datetime.utcnow()

        # A dead process's abandoned row would otherwise block this tenant
        # forever, via the one-RUNNING-per-tenant index.
        recover_stale_runs(session, tenant_id=tenant_id, now=started_at)

        run = self._start_run(session, tenant_id, started_at)
        logger.info(
            "Usage reconciliation started",
            extra={
                "event": "usage_reconciliation.started",
                "tenant_id": str(tenant_id),
                "reconciliation_id": str(run.id),
                "repair": repair,
                "verify_s3": verify_s3,
            },
        )

        try:
            snapshot = self._run_accounting(session, tenant_id, repair=repair)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Usage reconciliation accounting phase failed",
                extra={
                    "event": "usage_reconciliation.failed",
                    "tenant_id": str(tenant_id),
                    "reconciliation_id": str(run.id),
                    "error_code": ERROR_UNEXPECTED,
                },
            )
            return self._finish_failed(session, run, ERROR_UNEXPECTED)

        findings: Optional[S3IntegrityFindings] = None
        s3_error: Optional[str] = None
        if verify_s3:
            try:
                findings = self._verify_storage_integrity(
                    tenant_id, snapshot.billable_objects, snapshot.tracked_keys, run.id
                )
            except Exception as exc:  # noqa: BLE001
                s3_error = _classify_s3_error(exc)
                logger.error(
                    "Usage reconciliation S3 verification failed",
                    extra={
                        "event": "usage_reconciliation.failed",
                        "tenant_id": str(tenant_id),
                        "reconciliation_id": str(run.id),
                        "error_code": s3_error,
                    },
                )

        return self._finish(session, run, snapshot, findings, s3_error)

    # -- lifecycle ---------------------------------------------------------

    def _start_run(
        self, session: Session, tenant_id: UUID, started_at: datetime
    ) -> TenantUsageReconciliation:
        run = TenantUsageReconciliation(
            tenant_id=tenant_id,
            status=TenantUsageReconciliationStatus.RUNNING,
            started_at=started_at,
        )
        session.add(run)
        try:
            session.commit()
        except IntegrityError:
            # The partial unique index rejected a second RUNNING row. The
            # rollback matters as much as the raise: leaving the caller's
            # session in a failed transaction would poison every unrelated
            # statement that follows it on the same request.
            session.rollback()
            logger.info(
                "Usage reconciliation refused: another run is already active",
                extra={
                    "event": "usage_reconciliation.skipped",
                    "tenant_id": str(tenant_id),
                    "error_code": ERROR_CONCURRENT_RECONCILIATION,
                },
            )
            raise ConcurrentReconciliationError(tenant_id) from None
        session.refresh(run)
        return run

    def _finish_failed(
        self, session: Session, run: TenantUsageReconciliation, error_code: str
    ) -> ReconciliationOutcome:
        completed_at = datetime.utcnow()
        session.rollback()
        run = session.get(TenantUsageReconciliation, run.id)
        run.status = TenantUsageReconciliationStatus.FAILED
        run.completed_at = completed_at
        run.error_code = error_code
        session.add(run)
        session.commit()
        logger.error(
            "Usage reconciliation failed",
            extra={
                "event": "usage_reconciliation.failed",
                "tenant_id": str(run.tenant_id),
                "reconciliation_id": str(run.id),
                "error_code": error_code,
            },
        )
        return ReconciliationOutcome(
            reconciliation_id=run.id,
            tenant_id=run.tenant_id,
            status=str(TenantUsageReconciliationStatus.FAILED.value),
            started_at=run.started_at,
            completed_at=run.completed_at,
            expected_storage_bytes=None,
            actual_storage_bytes=None,
            difference_bytes=None,
            repaired=False,
            error_code=error_code,
        )

    def _finish(
        self,
        session: Session,
        run: TenantUsageReconciliation,
        snapshot: _AccountingSnapshot,
        findings: Optional[S3IntegrityFindings],
        s3_error: Optional[str],
    ) -> ReconciliationOutcome:
        completed_at = datetime.utcnow()
        session.rollback()
        run = session.get(TenantUsageReconciliation, run.id)
        run.completed_at = completed_at
        run.expected_storage_bytes = snapshot.expected_bytes
        run.actual_storage_bytes = snapshot.actual_bytes
        run.difference_bytes = snapshot.difference_bytes
        run.repaired = snapshot.repaired
        if findings is not None:
            run.objects_checked = findings.objects_checked
            run.orphans_found = findings.orphans_found
            run.missing_objects_found = findings.missing_objects_found
            run.metadata_mismatches_found = findings.metadata_mismatches_found
        if s3_error is not None:
            run.status = TenantUsageReconciliationStatus.FAILED
            run.error_code = s3_error
        else:
            run.status = TenantUsageReconciliationStatus.SUCCEEDED
        session.add(run)
        session.commit()

        outcome = ReconciliationOutcome(
            reconciliation_id=run.id,
            tenant_id=run.tenant_id,
            status=str(run.status.value if hasattr(run.status, "value") else run.status),
            started_at=run.started_at,
            completed_at=run.completed_at,
            expected_storage_bytes=run.expected_storage_bytes,
            actual_storage_bytes=run.actual_storage_bytes,
            difference_bytes=run.difference_bytes,
            repaired=bool(run.repaired),
            objects_checked=run.objects_checked,
            orphans_found=run.orphans_found,
            missing_objects_found=run.missing_objects_found,
            metadata_mismatches_found=run.metadata_mismatches_found,
            accepted_retained_objects=(
                findings.accepted_retained_objects if findings is not None else 0
            ),
            logo_integrity_errors=snapshot.logo_integrity_errors,
            legacy_logo_unresolved=snapshot.legacy_logo_unresolved,
            usage_initialized=snapshot.usage_initialized,
            error_code=run.error_code,
        )
        logger.info(
            "Usage reconciliation completed",
            extra={
                "event": "usage_reconciliation.completed",
                "tenant_id": str(run.tenant_id),
                "reconciliation_id": str(run.id),
                "status": outcome.status,
                "expected_storage_bytes": outcome.expected_storage_bytes,
                "actual_storage_bytes": outcome.actual_storage_bytes,
                "difference_bytes": outcome.difference_bytes,
                "repaired": outcome.repaired,
                "objects_checked": outcome.objects_checked,
                "orphans_found": outcome.orphans_found,
                "missing_objects_found": outcome.missing_objects_found,
                "metadata_mismatches_found": outcome.metadata_mismatches_found,
                "accepted_retained_objects": outcome.accepted_retained_objects,
                "logo_integrity_errors": outcome.logo_integrity_errors,
                "error_code": outcome.error_code,
            },
        )
        return outcome

    # -- accounting --------------------------------------------------------

    def _run_accounting(
        self, session: Session, tenant_id: UUID, *, repair: bool
    ) -> _AccountingSnapshot:
        """The locked, coherent accounting snapshot (and optional repair).

        Everything the later phases need is copied into frozen values before
        this transaction commits — see `_AccountingSnapshot`.
        """
        usage = session.exec(
            select(TenantUsage)
            .where(TenantUsage.tenant_id == tenant_id)
            .with_for_update()
        ).first()

        actual = StorageBillingService.compute_billable_storage_bytes(session, tenant_id)

        usage_initialized = False
        expected: Optional[int]
        difference: Optional[int]
        repaired = False

        if usage is None:
            # Missing `TenantUsage` is NOT "zero usage" (Block B's contract).
            # Recovery policy: initialize explicitly from the complete
            # authoritative baseline we just computed — the same number
            # Block C's initialization would have written — never from a
            # partial or incremental value. `expected`/`difference` stay
            # NULL because there was no counter to compare against; a `0`
            # there would be indistinguishable from a real zero counter.
            UsageService.initialize_usage(
                session,
                tenant_id,
                billable_storage_bytes=actual,
                source="usage_reconciliation_recovery",
            )
            usage_initialized = True
            repaired = True
            expected = None
            difference = None
            logger.warning(
                "Usage reconciliation initialized a missing TenantUsage row",
                extra={
                    "event": "usage_reconciliation.usage_initialized",
                    "tenant_id": str(tenant_id),
                    "actual_storage_bytes": actual,
                },
            )
        else:
            expected = usage.billable_storage_bytes
            difference = actual - expected
            if difference != 0 and repair:
                UsageService.adjust_storage(
                    session,
                    tenant_id,
                    difference,
                    source="usage_reconciliation",
                    resource_type="reconciliation",
                )
                repaired = True
                logger.warning(
                    "Usage reconciliation repaired a drifted storage counter",
                    extra={
                        "event": "usage_reconciliation.counter_repaired",
                        "tenant_id": str(tenant_id),
                        "expected_storage_bytes": expected,
                        "actual_storage_bytes": actual,
                        "difference_bytes": difference,
                    },
                )

        billable_objects = tuple(
            StorageBillingService.get_billable_storage_objects(session, tenant_id)
        )
        tracked_keys = frozenset(
            session.exec(
                select(StorageObject.object_key).where(
                    StorageObject.tenant_id == tenant_id
                )
            ).all()
        )
        logo_errors, legacy_unresolved = self._check_logo_integrity(session, tenant_id)

        session.commit()
        return _AccountingSnapshot(
            expected_bytes=expected,
            actual_bytes=actual,
            difference_bytes=difference,
            repaired=repaired,
            usage_initialized=usage_initialized,
            billable_objects=billable_objects,
            tracked_keys=tracked_keys,
            logo_integrity_errors=logo_errors,
            legacy_logo_unresolved=legacy_unresolved,
        )

    def _check_logo_integrity(
        self, session: Session, tenant_id: UUID
    ) -> tuple[int, bool]:
        """DB-scoped tenant-logo integrity (Block D §26/§27).

        A foreign key guarantees the referenced row *exists*; it does not
        guarantee the row belongs to this tenant or is a tenant-logo object
        at all. Both are checked here, and neither is repaired — repointing
        a logo FK is a data-integrity decision for a human, not something
        reconciliation may guess at.
        """
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            return 0, False

        if tenant.logo_storage_id is None:
            unresolved = bool(tenant.logo_url)
            if unresolved:
                logger.warning(
                    "Tenant has a legacy logo_url that no StorageObject resolves",
                    extra={
                        "event": "usage_reconciliation.legacy_logo_unresolved",
                        "tenant_id": str(tenant_id),
                        "error_code": "legacy_logo_reference_unresolved",
                    },
                )
            return 0, unresolved

        obj = session.get(StorageObject, tenant.logo_storage_id)
        problems = 0
        if obj is None:
            problems = 1
            reason = "missing_storage_object"
        elif obj.tenant_id != tenant_id:
            problems = 1
            reason = "cross_tenant_owner"
        elif not obj.object_key.startswith(tenant_logo_key_prefix(tenant_id)):
            problems = 1
            reason = "not_a_tenant_logo_object"
        else:
            reason = None

        if problems:
            logger.error(
                "Tenant logo FK does not reference a valid tenant-logo object",
                extra={
                    "event": "usage_reconciliation.logo_integrity_error",
                    "tenant_id": str(tenant_id),
                    "storage_object_id": str(tenant.logo_storage_id),
                    "error_code": "tenant_logo_integrity_error",
                    "reason": reason,
                },
            )
        return problems, False

    # -- S3 integrity ------------------------------------------------------

    def _verify_storage_integrity(
        self,
        tenant_id: UUID,
        billable_objects: Sequence[BillableStorageObjectRef],
        tracked_keys: frozenset[str],
        reconciliation_id: UUID,
    ) -> S3IntegrityFindings:
        """Read-only. HEADs every billable object, then lists the tenant's
        attributable prefixes looking for objects no `StorageObject` row
        tracks. Deletes nothing, updates nothing."""
        s3 = self._s3_client()

        objects_checked = 0
        missing = 0
        mismatches = 0
        seen: Set[str] = set()

        for ref in billable_objects:
            if ref.object_key in seen:
                # The same StorageObject can be reachable from more than one
                # billable relationship; HEADing it twice would inflate
                # objects_checked without checking anything new.
                continue
            seen.add(ref.object_key)
            objects_checked += 1

            head = s3.head_object(ref.object_key)
            if head is None:
                missing += 1
                logger.error(
                    "A billable StorageObject has no object in S3",
                    extra={
                        "event": "usage_reconciliation.missing_object",
                        "tenant_id": str(tenant_id),
                        "reconciliation_id": str(reconciliation_id),
                        "storage_object_id": str(ref.storage_object_id),
                        "category": ref.category,
                    },
                )
                continue

            if _metadata_mismatch(ref, head):
                mismatches += 1
                logger.warning(
                    "A billable StorageObject's metadata disagrees with S3",
                    extra={
                        "event": "usage_reconciliation.metadata_mismatch",
                        "tenant_id": str(tenant_id),
                        "reconciliation_id": str(reconciliation_id),
                        "storage_object_id": str(ref.storage_object_id),
                        "category": ref.category,
                        "db_size_bytes": ref.size_bytes,
                        "s3_size_bytes": head.size_bytes,
                        "etag_matches": _etag_matches(ref.etag, head.etag),
                    },
                )

        orphans, accepted = self._scan_for_orphans(
            s3, tenant_id, tracked_keys, reconciliation_id
        )

        return S3IntegrityFindings(
            objects_checked=objects_checked,
            missing_objects_found=missing,
            metadata_mismatches_found=mismatches,
            orphans_found=orphans,
            accepted_retained_objects=accepted,
        )

    def _scan_for_orphans(
        self,
        s3: S3Service,
        tenant_id: UUID,
        tracked_keys: frozenset[str],
        reconciliation_id: UUID,
    ) -> tuple[int, int]:
        """Physical S3 objects under this tenant's attributable prefixes
        that no `StorageObject` row tracks.

        Two outcomes, deliberately separated: a retained-after-delete
        signature PNG (R6) is accepted, by-design divergence and is counted
        on its own; anything else — most commonly a deleted sample image
        whose S3 object was never removed (R2) or a failed upload's residue
        (R4) — is a genuine orphan. Neither is billable, and neither is
        deleted here.
        """
        orphans = 0
        accepted = 0
        for prefix in TENANT_ATTRIBUTABLE_KEY_PREFIXES:
            for key in s3.iter_object_keys(f"{prefix}{tenant_id}/"):
                if key in tracked_keys:
                    continue
                if SIGNATURE_KEY_MARKER in key:
                    accepted += 1
                    logger.info(
                        "Accepted retained S3 object (signature retention policy)",
                        extra={
                            "event": "usage_reconciliation.accepted_retained_object",
                            "tenant_id": str(tenant_id),
                            "reconciliation_id": str(reconciliation_id),
                            "prefix": prefix,
                        },
                    )
                    continue
                orphans += 1
                logger.warning(
                    "S3 object with no StorageObject row detected",
                    extra={
                        "event": "usage_reconciliation.orphan_detected",
                        "tenant_id": str(tenant_id),
                        "reconciliation_id": str(reconciliation_id),
                        "prefix": prefix,
                    },
                )
        return orphans, accepted


#: A multipart ETag: the MD5 of the concatenated part checksums, followed by
#: the part count. Matched precisely rather than by "contains a hyphen" —
#: an ETag is otherwise an opaque provider string, and a loose test would
#: quietly stop comparing ETags that merely happen to contain one.
_MULTIPART_ETAG = re.compile(r"^[0-9a-fA-F]{32}-\d+$")


def _etag_matches(db_etag: Optional[str], s3_etag: Optional[str]) -> Optional[bool]:
    """Whether the two ETags agree, or `None` when the comparison is not
    meaningful.

    Not meaningful when either side is absent, or when either is a
    multipart ETag: that value is not the object's MD5 and depends on the
    part size the uploader happened to use, so comparing it against a
    single-part ETag would report every large upload as an integrity
    incident on every run, forever.
    """
    if not db_etag or not s3_etag:
        return None
    left = db_etag.strip('"')
    right = s3_etag.strip('"')
    if _MULTIPART_ETAG.match(left) or _MULTIPART_ETAG.match(right):
        return None
    return left == right


def _metadata_mismatch(ref: BillableStorageObjectRef, head) -> bool:
    """A size or ETag disagreement between the DB row and the live object.

    Only compares what both sides actually have: a NULL `size_bytes` in the
    DB is a metadata *gap*, not a mismatch (Block A's own distinction), and
    an incomparable ETag is not evidence of anything.
    """
    if ref.size_bytes is not None and head.size_bytes is not None:
        if ref.size_bytes != head.size_bytes:
            return True
    return _etag_matches(ref.etag, head.etag) is False
