"""Céluma 1.3.1 Block D (CEL-131-06) — the tenant-level default reviewer
FALLBACK.

`Tenant.default_reviewer_id` is a configuration reference, never a role, a
permission, or a grant of clinical authority (see
docs/celuma-1.3.1/block-d/default-reviewer-contract.md for the full contract).
This module owns the one place that reference is ever turned into a real
`ReportReview` assignment: `submit_report` in `app/api/v1/reports.py`, which
already hard-refuses (400) to submit a report for review when the order has
no reviewer assigned at all. That existing gate is the canonical moment a
missing reviewer already becomes consequential, so the fallback slots in
there rather than inventing a second assignment mechanism — see the
investigation recorded in the contract doc for why this moment was chosen
over order-creation or report-creation.

Two invariants this module exists to guarantee:

  * **Explicit assignment always wins.** The caller only invokes this when
    the order's reviewer list is already empty — never to replace, augment,
    or race a deliberate assignment made through
    `PUT /laboratory/orders/{order_id}/reviewers`.
  * **A stale default is never used.** Eligibility (exists, active, same
    tenant, currently holds the `reviewer` role) is revalidated live, at the
    moment of submission — never trusted from whenever the tenant setting was
    configured. A default that no longer qualifies silently resolves to "no
    fallback available", which preserves the existing, already-safe
    fail-closed behaviour (no reviewers -> 400) rather than fabricating
    authority for someone who no longer qualifies.
"""
from __future__ import annotations

from typing import Optional

from sqlmodel import Session

from app.core.rbac import ROLE_REVIEWER, get_user_roles
from app.models.report import Report
from app.models.report_review import ReportReview
from app.models.enums import ReviewStatus
from app.models.tenant import Tenant
from app.models.user import AppUser


def resolve_fallback_reviewer_assignment(
    session: Session, report: Report
) -> Optional[ReportReview]:
    """Returns a new, uncommitted `ReportReview` for the tenant's configured
    default reviewer, or `None` if there is no default configured or the
    configured user is no longer an eligible reviewer.

    Never queries or touches any existing `ReportReview` row — the caller is
    responsible for only calling this when the order has none at all. The
    returned row is added to the session but not flushed or committed; the
    caller does that as part of its own transaction (mirroring how
    `_sync_report_reviewers` in laboratory.py builds rows for the caller to
    add).
    """
    tenant = session.get(Tenant, report.tenant_id)
    if tenant is None or tenant.default_reviewer_id is None:
        return None

    candidate = session.get(AppUser, tenant.default_reviewer_id)
    if candidate is None:
        return None
    if str(candidate.tenant_id) != str(report.tenant_id):
        return None
    if not candidate.is_active:
        return None
    if ROLE_REVIEWER not in get_user_roles(candidate.id, session):
        return None

    return ReportReview(
        tenant_id=report.tenant_id,
        order_id=report.order_id,
        report_id=report.id,
        reviewer_user_id=candidate.id,
        # No human actor performed this assignment — the tenant configured a
        # fallback in advance. `assigned_by_user_id` is nullable for exactly
        # this ("optional" in the model docstring).
        assigned_by_user_id=None,
        status=ReviewStatus.PENDING,
    )
