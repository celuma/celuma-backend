"""Tenant usage read API and manual reconciliation trigger (Céluma 1.3,
Phase 4, Blocks D and E).

Two HTTP surfaces, one tenant-scoped router:

    GET  /api/v1/tenant/usage            -- Block E: the dashboard read
    POST /api/v1/tenant/usage/reconcile  -- Block D: the support escape hatch

Tenant scoping is structural, not checked: neither endpoint takes a tenant
identifier at all — not in the path, not in the query string, not in a
body. The tenant read (or reconciled) is always the authenticated caller's
own (`AppUser.tenant_id`), so a tenant admin has no representable way to
ask for another tenant's numbers. That is the strongest available form of
the "callers see only their own tenant" rule: there is no parameter to
validate, and therefore none to get wrong later.

Both endpoints are gated on `admin:manage_tenant` — the same permission the
tenant-settings and tenant-logo endpoints already use. Block E deliberately
does not introduce a new permission, and deliberately does not grant the
`billing` role access: Phase 4 defines this as tenant administration
information, not invoicing functionality (see usage-rbac-contract.md).

Content policy (both endpoints): aggregates only. No object key, no bucket,
no storage-object id, no report title, no presigned URL, no raw AWS message
and no patient-identifying value ever appears in a response body. The only
error information exposed is `error_code`, which is already a sanitized,
stable code by Block D's contract.
"""
from datetime import datetime
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from app.api.v1.auth import current_user
from app.core.db import get_session
from app.core.rbac import has_permission
from app.models.tenant_usage_reconciliation import TenantUsageReconciliation
from app.models.user import AppUser
from app.services.usage import UsageService
from app.services.usage_reconciliation import (
    ConcurrentReconciliationError,
    UsageReconciliationService,
)

router = APIRouter(prefix="/tenant/usage")


# ---------------------------------------------------------------------------
# Derived-value conventions (Céluma 1.3, Phase 4, Block E)
# ---------------------------------------------------------------------------

#: Decimal places for the UI-facing `usage_percent`. The unrounded quotient
#: is preserved separately as `usage_ratio`, so a caller that needs full
#: precision never has to reconstruct it from a rounded percentage.
PERCENT_DECIMALS = 2

#: `integrity_status` — a deterministic summary of the latest reconciliation,
#: computed here so Block F does not reimplement the rules (and cannot drift
#: from them).
INTEGRITY_NOT_RUN = "NOT_RUN"
INTEGRITY_RUNNING = "RUNNING"
INTEGRITY_FAILED = "FAILED"
INTEGRITY_WARNING = "WARNING"
INTEGRITY_HEALTHY = "HEALTHY"
#: A SUCCEEDED run whose S3 verification was disabled: the accounting half
#: is trustworthy, the integrity counters were never measured. Deliberately
#: NOT `HEALTHY` — a green light for a check that never ran would be a lie,
#: and NULL counters mean "not measured", never "none found".
INTEGRITY_ACCOUNTING_ONLY = "ACCOUNTING_ONLY"


def _ratio_and_percent(
    used: Optional[int], limit: Optional[int]
) -> Tuple[Optional[float], Optional[float]]:
    """`(usage_ratio, usage_percent)` for a used/limit pair.

    `None` for both whenever the quotient has no meaning: no limit
    configured (unlimited), or no usage number to divide (an uninitialized
    `TenantUsage` row). Never `0`, which would be indistinguishable from a
    real zero-usage tenant on a real limit.

    Ratios are NOT clamped: a tenant over its ceiling reports `1.23` /
    `123.0`, because Phase 4 observes over-limit states rather than hiding
    them, and nothing anywhere enforces a limit.
    """
    if used is None or limit is None:
        return None, None
    if limit <= 0:
        # Unreachable through the schema (`ck_tenant_limits_*_positive`
        # rejects zero and negative limits); a guard, not a policy.
        return None, None
    ratio = used / limit
    return ratio, round(ratio * 100, PERCENT_DECIMALS)


def _integrity_status(run: Optional[TenantUsageReconciliation]) -> str:
    """Deterministic health summary of the latest reconciliation run.

    The rules, in order:

        no run at all                      -> NOT_RUN
        status RUNNING                     -> RUNNING
        status FAILED                      -> FAILED
        SUCCEEDED, any integrity counter NULL -> ACCOUNTING_ONLY
        SUCCEEDED, any integrity counter > 0  -> WARNING
        SUCCEEDED, all integrity counters 0   -> HEALTHY

    `objects_checked` is deliberately not part of the decision: a tenant
    with no billable objects legitimately checks zero of them, and that is
    not a warning.
    """
    if run is None:
        return INTEGRITY_NOT_RUN

    status = _status_value(run.status)
    if status == "RUNNING":
        return INTEGRITY_RUNNING
    if status == "FAILED":
        return INTEGRITY_FAILED

    counters = (
        run.orphans_found,
        run.missing_objects_found,
        run.metadata_mismatches_found,
    )
    if any(counter is None for counter in counters):
        return INTEGRITY_ACCOUNTING_ONLY
    if any(counter > 0 for counter in counters):
        return INTEGRITY_WARNING
    return INTEGRITY_HEALTHY


def _status_value(status) -> str:
    """The persisted status as a plain string, whether SQLModel handed back
    the enum member or the raw column value."""
    return str(status.value if hasattr(status, "value") else status)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class StorageUsageResponse(BaseModel):
    """Current billable storage against the configured ceiling.

    `billable_bytes` is Céluma's **billable** storage total (Block B's
    `TenantUsage.billable_storage_bytes`), not the tenant's physical S3
    footprint — those differ by design. Bytes are the contract; no MB/GB/TB
    value is returned, and no GB/GiB ambiguity is introduced into backend
    accounting.
    """

    #: Whether a `TenantUsage` row exists. `false` means usage tracking has
    #: never been initialized for this tenant — NOT that the tenant uses
    #: zero bytes (Block B's load-bearing distinction).
    initialized: bool
    #: `None` exactly when `initialized` is `false`.
    billable_bytes: Optional[int] = None
    #: `None` when no storage limit is configured.
    limit_bytes: Optional[int] = None
    #: `true` when no storage limit is configured (no `TenantLimits` row, or
    #: a row whose `storage_limit_bytes` is NULL — the two are equivalent).
    unlimited: bool
    #: `billable_bytes / limit_bytes`, unrounded. `None` when either side is
    #: absent. Not clamped at 1.0.
    usage_ratio: Optional[float] = None
    #: `usage_ratio * 100`, rounded to 2 decimals. `None` with the ratio.
    usage_percent: Optional[float] = None


class UserUsageResponse(BaseModel):
    """Live user counts (Block B's `get_user_metrics`) against the seat
    ceiling.

    `active_internal_users` is the licensed-seat metric and the only one
    that consumes `user_limit`. `registered_users` (every row, any status)
    and `active_physician_portal_users` (portal-only physicians, disjoint
    from internal users by construction) are reported alongside it and
    never merged into it.
    """

    registered_users: int
    active_internal_users: int
    active_physician_portal_users: int
    #: `None` when no user limit is configured.
    user_limit: Optional[int] = None
    unlimited: bool
    #: `active_internal_users / user_limit`, unrounded. Not clamped at 1.0.
    usage_ratio: Optional[float] = None
    usage_percent: Optional[float] = None


class ReconciliationSummaryResponse(BaseModel):
    """The tenant's most recently *started* reconciliation run.

    Read-only and eventually consistent with the storage block above: the
    GET never triggers a run, never waits for one, and never HEADs or LISTs
    S3. A run in progress is reported as such.

    Every counter is nullable on purpose. `NULL` means "not measured"
    (verification disabled, or the run has not finished); `0` means
    "verified, none found". Collapsing the two would turn an unverified
    tenant into a clean bill of health.
    """

    #: `false` when reconciliation has never run for this tenant. Every
    #: other field is then `None`/`NOT_RUN` — never a fabricated clean state.
    has_run: bool
    #: Deterministic summary: NOT_RUN / RUNNING / FAILED / WARNING /
    #: HEALTHY / ACCOUNTING_ONLY.
    integrity_status: str
    #: RUNNING / SUCCEEDED / FAILED — the raw run status.
    status: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    #: What `TenantUsage.billable_storage_bytes` said at run start (`None`
    #: when the run had to initialize the row: there was no counter to
    #: compare).
    expected_storage_bytes: Optional[int] = None
    #: The authoritative **billable** total recomputed from DB
    #: relationships — NOT the tenant's physical S3 footprint. Block D's
    #: contract; do not relabel it.
    actual_storage_bytes: Optional[int] = None
    #: `actual - expected`; positive means the counter under-counted.
    difference_bytes: Optional[int] = None
    #: Whether that run corrected the counter.
    repaired: Optional[bool] = None

    objects_checked: Optional[int] = None
    orphans_found: Optional[int] = None
    missing_objects_found: Optional[int] = None
    metadata_mismatches_found: Optional[int] = None

    #: Sanitized, stable code only (`s3_access_denied`, `s3_timeout`,
    #: `s3_unavailable`, `unexpected_error`, `stale_run_recovered`) — never
    #: an exception message, a bucket, a key or an AWS response.
    error_code: Optional[str] = None


class TenantUsageResponse(BaseModel):
    """Everything a tenant-usage dashboard needs, in one read.

    Three conceptually distinct blocks, each independently sourced:
    storage from the `TenantUsage` counter, users from live `app_user`
    counts, reconciliation from the latest history row. They are
    observational values updated by independent operations and are
    **eventually consistent** with each other — this endpoint deliberately
    does not take a distributed snapshot across them.
    """

    storage: StorageUsageResponse
    users: UserUsageResponse
    reconciliation: ReconciliationSummaryResponse


class ReconciliationRunResponse(BaseModel):
    """Operational aggregates only.

    Deliberately carries no object key, no bucket, no storage-object id, no
    report title and no patient-identifying value — a reconciliation report
    is an operations artifact, and Block D's content policy is the same one
    the notification domain established. `actual_storage_bytes` is the
    authoritative *billable* total recomputed from DB relationships, NOT
    every physical byte in S3 (see reconciliation-api-contract.md §3).
    """

    reconciliation_id: str
    status: str
    started_at: datetime
    completed_at: Optional[datetime] = None

    expected_storage_bytes: Optional[int] = None
    actual_storage_bytes: Optional[int] = None
    difference_bytes: Optional[int] = None

    repaired: bool = False

    objects_checked: Optional[int] = None
    orphans_found: Optional[int] = None
    missing_objects_found: Optional[int] = None
    metadata_mismatches_found: Optional[int] = None

    error_code: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=TenantUsageResponse)
def read_current_tenant_usage(
    session: Session = Depends(get_session),
    user: AppUser = Depends(current_user),
):
    """The caller's own tenant usage (requires admin:manage_tenant).

    A cheap, O(1)-per-source read: the storage counter row, the limits row,
    the live user counts, and the latest reconciliation row. It never scans
    `storage_object`, never recomputes the authoritative billable total
    (`StorageBillingService`), never calls S3, and never triggers a
    reconciliation — the `TenantUsage` counter exists precisely so a
    dashboard load is a single-row lookup, and reconciliation is what keeps
    that counter honest.

    Nothing here enforces anything: a tenant over its limit is reported,
    not blocked, and no threshold or notification behavior exists (Block
    G's).
    """
    if not has_permission(user.id, "admin:manage_tenant", session):
        raise HTTPException(403, "Permission required: admin:manage_tenant")

    tenant_id = user.tenant_id

    usage = UsageService.get_usage(session, tenant_id)
    limits = UsageService.get_limits(session, tenant_id)
    metrics = UsageService.get_user_metrics(session, tenant_id)
    latest_run = UsageService.get_latest_reconciliation(session, tenant_id)

    # A missing `TenantLimits` row and a row with NULL fields mean the same
    # thing (Block B's contract), so both collapse to `None` here.
    storage_limit = limits.storage_limit_bytes if limits is not None else None
    user_limit = limits.user_limit if limits is not None else None

    billable_bytes = usage.billable_storage_bytes if usage is not None else None
    storage_ratio, storage_percent = _ratio_and_percent(billable_bytes, storage_limit)
    user_ratio, user_percent = _ratio_and_percent(
        metrics.active_internal_users, user_limit
    )

    storage = StorageUsageResponse(
        initialized=usage is not None,
        billable_bytes=billable_bytes,
        limit_bytes=storage_limit,
        unlimited=storage_limit is None,
        usage_ratio=storage_ratio,
        usage_percent=storage_percent,
    )
    users = UserUsageResponse(
        registered_users=metrics.registered_users,
        active_internal_users=metrics.active_internal_users,
        active_physician_portal_users=metrics.active_physician_portal_users,
        user_limit=user_limit,
        unlimited=user_limit is None,
        usage_ratio=user_ratio,
        usage_percent=user_percent,
    )

    if latest_run is None:
        reconciliation = ReconciliationSummaryResponse(
            has_run=False, integrity_status=INTEGRITY_NOT_RUN
        )
    else:
        reconciliation = ReconciliationSummaryResponse(
            has_run=True,
            integrity_status=_integrity_status(latest_run),
            status=_status_value(latest_run.status),
            started_at=latest_run.started_at,
            completed_at=latest_run.completed_at,
            expected_storage_bytes=latest_run.expected_storage_bytes,
            actual_storage_bytes=latest_run.actual_storage_bytes,
            difference_bytes=latest_run.difference_bytes,
            repaired=latest_run.repaired,
            objects_checked=latest_run.objects_checked,
            orphans_found=latest_run.orphans_found,
            missing_objects_found=latest_run.missing_objects_found,
            metadata_mismatches_found=latest_run.metadata_mismatches_found,
            error_code=latest_run.error_code,
        )

    return TenantUsageResponse(
        storage=storage, users=users, reconciliation=reconciliation
    )


@router.post("/reconcile", response_model=ReconciliationRunResponse)
def reconcile_current_tenant_usage(
    session: Session = Depends(get_session),
    user: AppUser = Depends(current_user),
):
    """Reconcile the caller's own tenant now (requires admin:manage_tenant).

    Runs synchronously: one tenant's reconciliation is bounded (a HEAD per
    billable object plus a listing sweep of four prefixes), and this
    codebase has no reliable fire-and-forget mechanism to hand it to —
    inventing one for a support tool would be worse than the wait. The
    scaling limit that follows (a very large tenant can outlast an HTTP or
    proxy timeout) is documented, not redesigned here.

    A run that fails (S3 unreachable, an unexpected error) still returns
    200 with `status: "FAILED"` and a sanitized `error_code`: the run
    happened and is recorded in `tenant_usage_reconciliation`, and the
    caller needs to see what it found. A *refused* run (another one is
    already active) is 409 — nothing happened and no history row was
    created.
    """
    if not has_permission(user.id, "admin:manage_tenant", session):
        raise HTTPException(403, "Permission required: admin:manage_tenant")

    try:
        outcome = UsageReconciliationService().reconcile_tenant(
            session, user.tenant_id
        )
    except ConcurrentReconciliationError:
        raise HTTPException(
            409, "A reconciliation is already running for this tenant"
        ) from None

    return ReconciliationRunResponse(
        reconciliation_id=str(outcome.reconciliation_id),
        status=outcome.status,
        started_at=outcome.started_at,
        completed_at=outcome.completed_at,
        expected_storage_bytes=outcome.expected_storage_bytes,
        actual_storage_bytes=outcome.actual_storage_bytes,
        difference_bytes=outcome.difference_bytes,
        repaired=outcome.repaired,
        objects_checked=outcome.objects_checked,
        orphans_found=outcome.orphans_found,
        missing_objects_found=outcome.missing_objects_found,
        metadata_mismatches_found=outcome.metadata_mismatches_found,
        error_code=outcome.error_code,
    )
