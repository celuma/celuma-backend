"""Recipient resolvers (Céluma 1.3, Phase 3, Block F, Story F3).

One function per event, each answering exactly one question: **which user ids
should receive this?** They return `list[UUID]` and nothing else — never an
email address, never an `AppUser`, never a role. Addresses are
`notification_delivery.py`'s business, read from the account row precisely so
that no caller can supply one (materialization contract §3).

The rules every resolver here obeys, from the Block A recipient matrix:

1. tenant-scoped by construction — every query filters on the event's tenant;
2. deterministic — plain SQL over already-persisted rows, no ambient state;
3. inactive users excluded (`AppUser.is_active`);
4. no blanket "all admins" fan-out anywhere (matrix cross-cutting rule 2);
5. no cross-tenant id can be returned, and `NotificationService` re-checks;
6. actor exclusion is *not* done here — `NotificationCommand.exclude_actor`
   defaults to true and the service applies it. Doing it twice would mean two
   places to get it wrong, and the service's version is the one covered by
   Block B's tests;
7. an empty result is legitimate and never an error (matrix rule 6).

On N+1
------
Every resolver is set-based: at most a handful of queries regardless of how
many recipients come back. `users_with_permission` in particular exists
because the obvious implementation of "reviewers who can sign" is
`[u for u in reviewers if has_permission(u.id, ...)]`, which is one
three-table join per reviewer.
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Set
from uuid import UUID

from sqlmodel import Session, select

from app.models.assignment import Assignment
from app.models.enums import AssignmentItemType
from app.models.permission import Permission
from app.models.report import Report, ReportVersion
from app.models.report_review import ReportReview
from app.models.role import Role
from app.models.role_permission import RolePermission
from app.models.user import AppUser
from app.models.user_role import UserRoleLink


# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------

def active_users_in_tenant(
    session: Session, user_ids: Iterable[UUID], tenant_id: UUID
) -> List[UUID]:
    """Filter `user_ids` down to active accounts in `tenant_id`.

    Order-preserving and duplicate-free, so a resolver's output is stable
    across runs — which is what lets a test assert an exact list rather than a
    set, and what keeps log lines comparable.

    This is the single choke point where a resolver's candidate set becomes a
    recipient set. Every resolver below ends with it, including the ones whose
    source table is already tenant-scoped: an assignment row cannot carry a
    foreign user today, but "cannot today" is not a guarantee worth relying on
    when the check is one `IN` clause.
    """
    ordered = _dedupe(user_ids)
    if not ordered:
        return []

    allowed: Set[UUID] = set(
        session.exec(
            select(AppUser.id).where(
                AppUser.id.in_(ordered),
                AppUser.tenant_id == tenant_id,
                AppUser.is_active == True,  # noqa: E712 — SQL boolean, not Python
            )
        ).all()
    )
    return [user_id for user_id in ordered if user_id in allowed]


def users_with_permission(
    session: Session, user_ids: Sequence[UUID], permission_code: str
) -> List[UUID]:
    """Filter `user_ids` down to those holding `permission_code`.

    One join over the whole candidate set, not one `has_permission()` call per
    user. `app/core/rbac.py`'s helpers are per-user by design (they answer "may
    *this* request proceed"); a recipient resolver asks the plural question and
    needs the plural query.
    """
    ordered = _dedupe(user_ids)
    if not ordered:
        return []

    holders: Set[UUID] = set(
        session.exec(
            select(UserRoleLink.user_id)
            .join(Role, Role.id == UserRoleLink.role_id)
            .join(RolePermission, RolePermission.role_id == Role.id)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(
                UserRoleLink.user_id.in_(ordered),
                Permission.code == permission_code,
            )
        ).all()
    )
    return [user_id for user_id in ordered if user_id in holders]


def _dedupe(user_ids: Iterable[UUID]) -> List[UUID]:
    seen: Set[UUID] = set()
    ordered: List[UUID] = []
    for user_id in user_ids:
        if user_id is not None and user_id not in seen:
            seen.add(user_id)
            ordered.append(user_id)
    return ordered


# ---------------------------------------------------------------------------
# Source sets
# ---------------------------------------------------------------------------

def order_reviewer_ids(
    session: Session, *, tenant_id: UUID, order_id: UUID
) -> List[UUID]:
    """Users listed as reviewers on the order, read fresh.

    `ReportReview` rather than `Assignment`: reviewers were decoupled from the
    assignment table (see `app/models/assignment.py`'s docstring), and
    `_sync_report_reviewers` writes here. Reading the wrong table would
    resolve nobody and the notification would silently have no recipients.

    Read at notification time rather than taken from the request, per matrix
    rule 3 — the recipient set is whoever is a reviewer at the instant the
    transition commits.
    """
    return list(
        session.exec(
            select(ReportReview.reviewer_user_id)
            .where(
                ReportReview.tenant_id == tenant_id,
                ReportReview.order_id == order_id,
            )
            .order_by(ReportReview.assigned_at, ReportReview.id)
        ).all()
    )


def order_assignee_ids(
    session: Session, *, tenant_id: UUID, order_id: UUID
) -> List[UUID]:
    """Staff currently assigned to the order.

    `unassigned_at IS NULL` is the live-assignment predicate: `_sync_assignments`
    soft-unassigns, so ignoring it would notify everyone who was *ever*
    assigned — a fan-out that grows monotonically for the life of the order.
    """
    return list(
        session.exec(
            select(Assignment.assignee_user_id)
            .where(
                Assignment.tenant_id == tenant_id,
                Assignment.item_type == AssignmentItemType.LAB_ORDER,
                Assignment.item_id == order_id,
                Assignment.unassigned_at == None,  # noqa: E711 — SQL NULL test
            )
            .order_by(Assignment.assigned_at, Assignment.id)
        ).all()
    )


def report_author_ids(session: Session, *, report: Report) -> List[UUID]:
    """The report's author(s): its creator, plus the current version's author.

    Two fields because they can legitimately differ — `Report.created_by` is
    whoever started the report, `ReportVersion.authored_by` whoever wrote the
    version being acted on, and after a request-changes cycle that may be
    someone else. Both are "the person whose work this is", both belong in the
    recipient set, and `active_users_in_tenant` collapses them when they are
    the same person.
    """
    candidates: List[UUID] = []
    if report.created_by:
        candidates.append(report.created_by)

    current_author = session.exec(
        select(ReportVersion.authored_by).where(
            ReportVersion.report_id == report.id,
            ReportVersion.is_current == True,  # noqa: E712
        )
    ).first()
    if current_author:
        candidates.append(current_author)
    return _dedupe(candidates)


# ---------------------------------------------------------------------------
# Per-event resolvers
# ---------------------------------------------------------------------------

def resolve_report_submitted_recipients(
    session: Session, *, tenant_id: UUID, order_id: UUID
) -> List[UUID]:
    """REPORT_SUBMITTED — the order's reviewers.

    Matrix: "users listed as reviewers on the order at submit time". Not
    admins, not every pathologist: the notification says *you* were asked to
    review this, and it is true only of the people the order actually routes
    to. The submitting author is removed by the service's actor exclusion,
    including when the author is also a reviewer on their own submission.
    """
    return active_users_in_tenant(
        session,
        order_reviewer_ids(session, tenant_id=tenant_id, order_id=order_id),
        tenant_id,
    )


def resolve_report_pdf_ready_recipients(
    session: Session, *, tenant_id: UUID, order_id: UUID
) -> List[UUID]:
    """REPORT_PDF_READY — reviewers who can actually sign.

    Matrix: "users holding `reports:sign` **and** currently assigned as a
    reviewer on the order". The intersection mirrors who may perform the next
    step, so the notification is addressed to people for whom it is
    actionable. A reviewer without `reports:sign` cannot act on a ready PDF
    and does not need to be told it exists; a signer who is not on this order
    is not this order's business.

    The permission filter runs on the (small) reviewer set, never on the
    tenant's user table.
    """
    reviewers = active_users_in_tenant(
        session,
        order_reviewer_ids(session, tenant_id=tenant_id, order_id=order_id),
        tenant_id,
    )
    return users_with_permission(session, reviewers, "reports:sign")


def resolve_report_published_recipients(
    session: Session, *, tenant_id: UUID, order_id: UUID, report: Report
) -> List[UUID]:
    """REPORT_PUBLISHED — the order's assignees plus the report's author.

    Matrix: staff recipients are "(a) order assignees; (b) the report author",
    with the signer removed by actor exclusion. The author is included
    explicitly rather than assumed to be an assignee, because authoring a
    report does not assign you to its order and the matrix's fallback ("if the
    assignee list is empty, still notify the report author") only works if the
    author is a first-class member of the set.

    The requesting physician is **not** here. Block F Story F12 selected
    Option A — see phase-3-block-f-architecture-decision.md §7: an
    account-less physician has nowhere to log in, and the built delivery
    pipeline derives email recipients from in-app recipient rows, so a
    physician with an account could not be given "email only" as the matrix
    specifies without also giving them an inbox row they can never see.
    """
    candidates = order_assignee_ids(session, tenant_id=tenant_id, order_id=order_id)
    candidates.extend(report_author_ids(session, report=report))
    return active_users_in_tenant(session, candidates, tenant_id)


def resolve_report_retracted_recipients(
    session: Session, *, tenant_id: UUID, order_id: UUID, report: Report
) -> List[UUID]:
    """REPORT_RETRACTED — the same staff set as publication.

    Matrix: "same staff set as Report Published for the same order". Anyone
    who could have been told a report was published must be reachable when it
    is withdrawn; a narrower retraction set would leave someone believing a
    retracted report is still valid, which is the one failure in this matrix
    that is actively harmful.
    """
    return resolve_report_published_recipients(
        session, tenant_id=tenant_id, order_id=order_id, report=report
    )


def resolve_assignment_added_recipients(
    session: Session, *, tenant_id: UUID, added_user_ids: Sequence[UUID]
) -> List[UUID]:
    """ASSIGNMENT_ADDED — only the newly added users.

    Matrix: "**only** the newly added user(s)", from the `added` set already
    computed server-side by `_sync_assignments`. Not the removed set, not the
    people already assigned, not the whole resulting roster: a PUT that adds
    one person to a five-person order is one notification, not five.

    The actor is excluded by the service when they added themselves.
    """
    return active_users_in_tenant(session, added_user_ids, tenant_id)


def resolve_sample_status_changed_recipients(
    session: Session,
    *,
    tenant_id: UUID,
    order_id: UUID,
    report: Optional[Report] = None,
) -> List[UUID]:
    """SAMPLE_STATUS_CHANGED — order assignees, plus the report author if a
    report exists.

    Matrix, verbatim, and deliberately **not** widened to the sample's own
    assignees even though they are arguably the most interested party. This is
    the highest-frequency event in the set and the one Block D singled out as
    the concrete instance of the notification-storm risk (delivery policy
    registry, `SAMPLE_STATUS_CHANGED` note); adding a second source set here
    would multiply the fan-out of the event that can least afford it. Recorded
    as a deliberate conservative choice in
    notification-recipient-resolution-contract.md, not an oversight.

    In-app only — the policy registry gives this type `email_supported =
    False`, so no delivery row can exist for it whatever any preference says.
    """
    candidates = order_assignee_ids(session, tenant_id=tenant_id, order_id=order_id)
    if report is not None:
        candidates.extend(report_author_ids(session, report=report))
    return active_users_in_tenant(session, candidates, tenant_id)
