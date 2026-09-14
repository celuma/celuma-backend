"""Céluma 1.3.1 Block D (CEL-131-06) — the tenant default reviewer.

`Tenant.default_reviewer_id` is a configuration reference, never a role, a
permission, or a grant of clinical authority (see
docs/celuma-1.3.1/block-d/default-reviewer-contract.md for the full contract).
This module owns the one place that reference is ever turned into a real
`ReportReview` assignment.

## When it materializes — amended by the manual-validation remediation (R8)

Block D chose ONE moment: report submission, because that is where the
existing hard gate ("cannot submit without reviewers") already lived. Manual
validation rejected that as the *primary* moment. The consequence the block
did not anticipate is R1: an assigned reviewer may now configure a report's
presentation while it is still a DRAFT — and a laboratory whose reviewer is
configured as the tenant default could not use that, because no assignment
existed until submission. The order also, correctly but confusingly, read
"Sin revisores asignados" for the whole authoring phase.

The product contract is now:

    a valid tenant default reviewer is assigned to an order AT ORDER
    CREATION, and submission keeps an idempotent safety net.

Two call sites, ONE implementation — `ensure_default_reviewer_assignment`:

  * **order creation** (`create_order`, `create_order_unified`) — the primary
    materialization point;
  * **submit for review** (`submit_report`) — the safety net, which still
    matters for orders created before this release, orders created while no
    default was configured, a default configured after the order existed, and
    any other legitimate order that reaches submission with no reviewer.

The helper is **idempotent by construction**: it refuses to act when the order
already has any `ReportReview` row, so the second call site can never produce a
duplicate, and a retried request cannot either.

## Two invariants this module exists to guarantee

  * **Explicit assignment always wins.** The helper consults the tenant
    default only when the order has NO reviewer at all — never to replace,
    augment, or race a deliberate assignment made through
    `PUT /laboratory/orders/{order_id}/reviewers`.
  * **A stale default is never used.** Eligibility (exists, active, same
    tenant, currently holds the `reviewer` role) is revalidated live at the
    moment it matters — never trusted from whenever the tenant setting was
    configured. A default that no longer qualifies resolves to "no assignment"
    silently: at order creation the order is created with no reviewer, and at
    submission the pre-existing fail-closed 400 stands.

Nothing here grants authority. The row it produces is an ordinary assignment,
indistinguishable from a human's and governed by exactly the same Block A
locks.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from sqlmodel import Session, select

from app.core.rbac import ROLE_REVIEWER, get_user_roles
from app.models.enums import ReviewStatus
from app.models.report_review import ReportReview
from app.models.tenant import Tenant
from app.models.user import AppUser


def order_has_any_reviewer(session: Session, tenant_id, order_id) -> bool:
    """Whether the order already has ANY reviewer assignment, in any status.

    The same question `submit_report`'s pre-existing guard asks, and the one
    that makes an explicit assignment win: a decided (APPROVED/REJECTED) row
    counts exactly as much as a PENDING one, because it is still an
    assignment.
    """
    return (
        session.exec(
            select(ReportReview).where(
                ReportReview.tenant_id == _as_uuid(tenant_id),
                ReportReview.order_id == _as_uuid(order_id),
            )
        ).first()
        is not None
    )


def resolve_eligible_default_reviewer(
    session: Session, tenant_id
) -> Optional[AppUser]:
    """The tenant's configured default reviewer, if and only if that pointer
    still resolves to someone eligible RIGHT NOW.

    Four checks, re-run live every time — the configuration is a pointer that
    can go stale after it was set: the user's `reviewer` role can be revoked,
    the user can be deactivated, or (defensively checked, and impossible
    through the configuration endpoint) the pointer could cross tenants.

    Returns `None`, never raises, for every failure. A stale default must be
    indistinguishable from no default at all: it must not break order
    creation, and at submission it must fall through to the pre-existing
    fail-closed error rather than fabricating authority for someone who no
    longer qualifies.
    """
    tenant = session.get(Tenant, _as_uuid(tenant_id))
    if tenant is None or tenant.default_reviewer_id is None:
        return None

    candidate = session.get(AppUser, tenant.default_reviewer_id)
    if candidate is None:
        return None
    if str(candidate.tenant_id) != str(tenant_id):
        return None
    if not candidate.is_active:
        return None
    if ROLE_REVIEWER not in get_user_roles(candidate.id, session):
        return None
    return candidate


def ensure_default_reviewer_assignment(
    session: Session,
    *,
    tenant_id,
    order_id,
    report_id=None,
) -> Optional[ReportReview]:
    """THE canonical materialization. Adds at most one PENDING `ReportReview`
    for the tenant's configured default reviewer and returns it, or returns
    `None` when nothing should be created.

    `None` — and no row — when any of these holds:

        the order already has a reviewer assignment   (explicit wins; also
                                                       what makes this
                                                       idempotent)
        no default is configured
        the configured default is no longer eligible

    `report_id` is optional because at order creation no report exists yet.
    That is not a special case: `report_review.report_id` is nullable
    precisely so reviewers can be assigned before a report does
    (`_sync_report_reviewers` already creates such rows), authorization
    matches on `order_id` throughout (`find_any_review`), and the worklist
    falls back to resolving the report by `order_id` when the column is null.

    The row is added to the session but NOT flushed or committed — the caller
    owns its own transaction, mirroring how `_sync_report_reviewers` in
    `laboratory.py` builds rows for its caller to add.
    """
    if order_has_any_reviewer(session, tenant_id, order_id):
        return None

    candidate = resolve_eligible_default_reviewer(session, tenant_id)
    if candidate is None:
        return None

    review = ReportReview(
        tenant_id=_as_uuid(tenant_id),
        order_id=_as_uuid(order_id),
        report_id=_as_uuid(report_id) if report_id is not None else None,
        reviewer_user_id=candidate.id,
        # No human actor performed this assignment — the tenant configured a
        # fallback in advance. `assigned_by_user_id` is nullable for exactly
        # this ("optional" in the model docstring).
        assigned_by_user_id=None,
        status=ReviewStatus.PENDING,
    )
    session.add(review)
    return review


def _as_uuid(value):
    """Accept either a `UUID` or its string form.

    Call sites differ: routes carry `ctx.tenant_id` as a string while ORM
    objects carry real `UUID`s, and `ReportReview`'s columns are typed. This
    is the one place that difference is absorbed, so no caller has to
    remember.
    """
    if value is None or isinstance(value, UUID):
        return value
    return UUID(str(value))
