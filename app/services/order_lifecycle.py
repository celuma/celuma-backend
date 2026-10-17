"""Shared lifecycle guard for direct order/sample management (Céluma 1.4, issues #9, #10, #11).

Direct edits, sample-code corrections and erroneous-capture removal are allowed ONLY from Reception
through Diagnosis inclusive (RECEIVED, PROCESSING, DIAGNOSIS) and ONLY while no related report has
any review, approval, signing or publication evidence. The aggregate `order.status` is necessary but
never sufficient: an order returned from review is back in DIAGNOSIS with a DRAFT report, and must
stay protected because the history proves it was already reviewed.

Atomicity. Every operation that can move an order toward review, approval, signing or publication
takes the SAME lock first: the `order` row (`SELECT ... FOR NO KEY UPDATE`), via `lock_order`.
`FOR NO KEY UPDATE` conflicts with every other lifecycle lock but NOT with the `FOR KEY SHARE` that a
foreign-key check takes when a sample, event or report row is inserted for the order, so ordinary
child inserts never queue behind (or deadlock with) a management operation. The guard
therefore reads state that no concurrent submission/approval/signature can change until this
transaction ends, and a submission that arrives later waits and then sees the new data. The lock is
taken after the idempotency lock and the (waiting) code lock of a sample-code correction, and
before report rows, so no wait cycle exists: generation only ever TRIES code locks and never waits for one. Administrative status is not clinical authority: nothing here grants any permission; the
callers check capability, tenant and assignment themselves.

Contract: `celuma-engineering/specs/laboratory/order-sample-management.md`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm.exc import ObjectDeletedError
from sqlmodel import Session, select

from app.core.soft_removal import INCLUDE_REMOVED
from app.models.enums import EventType, OrderStatus, ReportStatus, ReviewStatus
from app.models.events import OrderEvent
from app.models.laboratory import Order
from app.models.report import Report, ReportVersion
from app.models.report_review import ReportReview

#: The confirmed window (Reception through Diagnosis inclusive).
DIRECT_MANAGEMENT_STATUSES = (OrderStatus.RECEIVED, OrderStatus.PROCESSING, OrderStatus.DIAGNOSIS)

#: Timeline events that prove a report entered the review/approval/signing/publication path.
#: They survive a "changes requested" return to DRAFT, which is exactly why they are consulted.
REVIEW_EVIDENCE_EVENTS = (
    EventType.REPORT_SUBMITTED,
    EventType.REPORT_APPROVED,
    EventType.REPORT_CHANGES_REQUESTED,
    EventType.REPORT_SIGNED,
    EventType.REPORT_PUBLISHED,
    EventType.REPORT_RETRACTED,
)

NOT_MODIFIABLE = "order_not_directly_modifiable"


@dataclass
class Blocker:
    code: str
    message: str
    report_id: Optional[str] = None

    def as_dict(self) -> dict:
        data = {"code": self.code, "message": self.message}
        if self.report_id:
            data["report_id"] = self.report_id
        return data


@dataclass
class Eligibility:
    blockers: List[Blocker] = field(default_factory=list)
    report_ids: List[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return not self.blockers


class LifecycleError(HTTPException):
    """409 `order_not_directly_modifiable` with every blocker, so the UI can say exactly why."""

    def __init__(self, blockers: List[Blocker], message: Optional[str] = None):
        super().__init__(409, {
            "code": NOT_MODIFIABLE,
            "message": message or "This order can no longer be changed directly: " + "; ".join(b.message for b in blockers),
            "blockers": [b.as_dict() for b in blockers],
        })


def lock_order(session: Session, order_id) -> None:
    """Take the order's lifecycle lock (held until commit/rollback). Raw SQL: the lock must be
    taken even for a tombstoned row, whose state the caller then reports."""
    session.execute(text('SELECT 1 FROM "order" WHERE id = :i FOR NO KEY UPDATE'), {"i": str(order_id)})


def lock_report_order(session: Session, report: Report) -> None:
    """Lifecycle operations on a report (submit, approve, request changes, reopen, sign, publish,
    retract, create) call this right after loading and tenant-checking the report: they take the
    same order lock as the management guard and then re-read the report, whose state may have
    changed while they waited."""
    lock_order(session, report.order_id)
    try:
        session.refresh(report)
    except (InvalidRequestError, ObjectDeletedError):
        raise HTTPException(404, "Report not found") from None
    # A column refresh is NOT filtered by the removed-record criteria: check the tombstone explicitly
    # (the order, and with it this draft report, may have been removed while we waited for the lock).
    if report.removed_at is not None:
        raise HTTPException(404, "Report not found")


def lock_and_refresh_order(session: Session, order: Order) -> None:
    """Same lock for operations that start from an already loaded order (creating its report):
    take it, then re-read the order, which may have been removed while we waited."""
    lock_order(session, order.id)
    try:
        session.refresh(order)
    except (InvalidRequestError, ObjectDeletedError):
        raise HTTPException(404, "Order not found") from None
    # A column refresh is NOT filtered by the removed-record criteria: check the tombstone explicitly.
    if order.removed_at is not None:
        raise HTTPException(404, "Order not found")


def _inspect(session: Session, order: Order) -> Eligibility:
    """Read-only evaluation of every condition. Reads bypass the removed-record filter and the
    identity map (`populate_existing`) so a locked, concurrent state is never masked."""
    opts = {INCLUDE_REMOVED: True, "populate_existing": True}
    result = Eligibility()

    if order.removed_at is not None:
        result.blockers.append(Blocker("order_removed", "The order was removed as an erroneous capture"))
        return result
    if order.status not in DIRECT_MANAGEMENT_STATUSES:
        result.blockers.append(Blocker(
            "order_phase",
            f"The order is in {order.status.value if hasattr(order.status, 'value') else order.status}; direct changes "
            "are only allowed from Reception through Diagnosis",
        ))

    reports = session.exec(
        select(Report).where(Report.order_id == order.id).order_by(Report.id).execution_options(**opts)
    ).all()
    result.report_ids = [str(r.id) for r in reports]
    for report in reports:
        rid = str(report.id)
        if report.status != ReportStatus.DRAFT:
            result.blockers.append(Blocker(
                "report_not_draft", f"A related report is {report.status.value}, not a draft", rid))
        if report.published_at is not None:
            result.blockers.append(Blocker("report_published", "A related report was published", rid))
        versions = session.exec(
            select(ReportVersion).where(ReportVersion.report_id == report.id).execution_options(**opts)
        ).all()
        if any(
            v.signed_at is not None or v.signed_by is not None or v.pdf_storage_id is not None
            or v.pdf_sha256 is not None or v.publish_started_at is not None
            for v in versions
        ):
            result.blockers.append(Blocker(
                "report_signing_evidence",
                "A related report version has signing, publication or issued-PDF evidence", rid))

    decided = session.exec(
        select(ReportReview.id).where(
            ReportReview.order_id == order.id,
            (ReportReview.status != ReviewStatus.PENDING) | ReportReview.decision_at.is_not(None),
        ).limit(1).execution_options(**opts)
    ).first()
    if decided is not None:
        result.blockers.append(Blocker(
            "report_review_evidence", "A reviewer already recorded a decision on a related report"))
    reviewed = session.exec(
        select(OrderEvent.id).where(
            OrderEvent.order_id == order.id, OrderEvent.event_type.in_(REVIEW_EVIDENCE_EVENTS)
        ).limit(1).execution_options(**opts)
    ).first()
    if reviewed is not None:
        result.blockers.append(Blocker(
            "report_review_history",
            "A related report was already submitted for review, approved, signed or published at some point; "
            "use the correction or amendment flow",
        ))
    # de-duplicate (several versions/reports can raise the same blocker)
    unique, seen = [], set()
    for b in result.blockers:
        key = (b.code, b.report_id)
        if key not in seen:
            seen.add(key)
            unique.append(b)
    result.blockers = unique
    return result


def evaluate(session: Session, order: Order) -> Eligibility:
    """Non-locking evaluation, for the "what can I do" read model. Never a substitute for
    `lock_and_guard` before a write."""
    return _inspect(session, order)


def lock_and_guard(session: Session, order_id, tenant_id) -> Order:
    """Lock the order, then check the whole policy against the locked state.

    Returns the freshly read `Order`. Raises 404 for an unknown/foreign/removed order and
    `LifecycleError` (409) for any blocker. The lock stays held, so the caller's write is atomic
    with this check.
    """
    lock_order(session, order_id)
    order = session.exec(
        select(Order).where(Order.id == order_id, Order.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    ).first()
    if order is None:
        raise HTTPException(404, "Order not found")
    eligibility = _inspect(session, order)
    if not eligibility.allowed:
        raise LifecycleError(eligibility.blockers)
    return order
