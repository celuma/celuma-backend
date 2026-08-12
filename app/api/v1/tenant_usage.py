"""Manual tenant-usage reconciliation trigger (Céluma 1.3, Phase 4, Block D).

The single HTTP surface this block adds: an operator/support escape hatch to
force a reconciliation run for the caller's own tenant, without waiting for
the worker's (hours-long, default-disabled) interval. Block A's architecture
proposal §3 recommends exactly this as the worker's companion, not its
replacement — both call the same `UsageReconciliationService`, so there is
one implementation of what a reconciliation is.

Tenant scoping is structural, not checked: this endpoint takes no tenant
identifier at all — not in the path, not in the query string, not in a
body. The tenant reconciled is always the authenticated caller's own
(`AppUser.tenant_id`), so a tenant admin has no representable way to ask
for another tenant's numbers. That is the strongest available form of the
"callers reconcile only their own tenant" rule Block D requires: there is
no parameter to validate, and therefore none to get wrong later.

The general usage-read API (current usage, limits, user metrics, latest
reconciliation) is Block E's, not this block's. Nothing here reads usage for
display.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from app.api.v1.auth import current_user
from app.core.db import get_session
from app.core.rbac import has_permission
from app.models.user import AppUser
from app.services.usage_reconciliation import (
    ConcurrentReconciliationError,
    UsageReconciliationService,
)

router = APIRouter(prefix="/tenant/usage")


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


@router.post("/reconcile", response_model=ReconciliationRunResponse)
def reconcile_current_tenant_usage(
    session: Session = Depends(get_session),
    user: AppUser = Depends(current_user),
):
    """Reconcile the caller's own tenant now (requires admin:manage_tenant).

    Runs synchronously: one tenant's reconciliation is bounded (a HEAD per
    billable object plus a listing sweep of four prefixes), and this
    codebase has no reliable fire-and-forget mechanism to hand it to —
    inventing one for a support tool would be worse than the wait.

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
