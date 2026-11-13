"""Shared lifecycle guard for direct order/sample management (Céluma 1.4, issues #9, #10, #11).

Direct edits, sample-code corrections and erroneous-capture removal are allowed ONLY from Reception
through Diagnosis inclusive. Order-level actions stay locked by any review evidence. Sample actions
may resume after an unsigned report returns for corrections or an issued report is retracted.

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
from app.models.report_amendment import AMENDMENT_STATE_OPEN, ReportAmendment
from app.models.report_review import ReportReview
from app.services.report_review_withdrawal import ACTION_REVIEW_WITHDRAWN

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

#: Guard scopes. ORDER is every order-level direct change (edit, notes, template, order removal). SAMPLE
#: is the sample-level management (code correction, date correction, sample removal) and is the ONLY scope
#: that honours the 1.4.4 retraction exception (see `_inspect`).
SCOPE_ORDER = "order"
SCOPE_SAMPLE = "sample"

#: Céluma 1.4.4: sample creation on an existing order is refused while a report is under review.
ORDER_IN_REVIEW = "order_in_review"
ACTIVE_REVIEW_REPORT_STATUSES = (ReportStatus.IN_REVIEW, ReportStatus.APPROVED)


def order_status_for_report(order: Order, report: Report) -> OrderStatus:
    if report.status in ACTIVE_REVIEW_REPORT_STATUSES:
        return OrderStatus.REVIEW
    if report.status == ReportStatus.PUBLISHED:
        return OrderStatus.CLOSED if order.billed_lock else OrderStatus.RELEASED
    return OrderStatus.DIAGNOSIS


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
    #: Reports whose retraction waived their issuance evidence for a SAMPLE-scope action (audit trail).
    retraction_exception_report_ids: List[str] = field(default_factory=list)

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


def _event_report_id(event_metadata) -> Optional[str]:
    value = (event_metadata or {}).get("report_id") if isinstance(event_metadata, dict) else None
    return str(value) if value else None


def _inspect(session: Session, order: Order, scope: str = SCOPE_ORDER) -> Eligibility:
    """Read-only evaluation of every condition. Reads bypass the removed-record filter and the
    identity map (`populate_existing`) so a locked, concurrent state is never masked.

    Céluma 1.4.4 retraction exception (SAMPLE scope only). A report that was formally RETRACTED no longer
    represents a live issuance: its issued JSON/PDF, signatures, review decisions and history stay untouched
    in storage, but they stop locking sample-level actions, because the published content is already
    withdrawn. The exception is deliberately narrow:

    * only the report's own issuance evidence is waived (`report_not_draft`, `report_published`,
      `report_signing_evidence`, and the review decisions/timeline events attributable to it);
    * an active signing/publication claim, a generating PDF or an open amendment on that report still
      blocks (`report_operation_active`, `report_amendment_open`);
    * any OTHER report of the order that is not retracted keeps every one of its blockers, and then
      evidence that cannot be attributed to a retracted report blocks too;
    * the order must still be in the Reception-Diagnosis window (`order_phase`), and the ORDER scope
      never waives anything.
    """
    opts = {INCLUDE_REMOVED: True, "populate_existing": True}
    result = Eligibility()

    if order.removed_at is not None:
        result.blockers.append(Blocker("order_removed", "The order was removed as an erroneous capture"))
        return result
    reports = session.exec(
        select(Report).where(Report.order_id == order.id).order_by(Report.id).execution_options(**opts)
    ).all()
    result.report_ids = [str(r.id) for r in reports]
    retracted_ids = (
        {str(r.id) for r in reports if r.status == ReportStatus.RETRACTED} if scope == SCOPE_SAMPLE else set()
    )
    has_other_report = any(str(r.id) not in retracted_ids for r in reports)

    event_types = (*REVIEW_EVIDENCE_EVENTS, EventType.STATUS_CHANGED) if scope == SCOPE_SAMPLE else REVIEW_EVIDENCE_EVENTS
    events = session.exec(
        select(OrderEvent.event_type, OrderEvent.event_metadata)
        .where(OrderEvent.order_id == order.id, OrderEvent.event_type.in_(event_types))
        .order_by(OrderEvent.created_at, OrderEvent.id)
        .execution_options(**opts)
    ).all()
    latest_review_events = {}
    for event_type, metadata in events:
        rid = _event_report_id(metadata)
        if rid and (event_type in REVIEW_EVIDENCE_EVENTS or (
            event_type == EventType.STATUS_CHANGED and metadata.get("action") == ACTION_REVIEW_WITHDRAWN
        )):
            latest_review_events[rid] = event_type

    # Only a recorded return for corrections of this draft waives its
    # submission/rejection history. Approval and issuance evidence still blocks.
    returned_drafts = {
        str(report.id) for report in reports
        if scope == SCOPE_SAMPLE and report.status == ReportStatus.DRAFT
        and latest_review_events.get(str(report.id)) in (EventType.REPORT_CHANGES_REQUESTED, EventType.STATUS_CHANGED)
    }

    # Older builds could leave REVIEW on the order after a request for changes.
    # Other reports and issuance evidence are still evaluated below.
    sample_return_to_diagnosis = order.status == OrderStatus.REVIEW and bool(returned_drafts)
    if order.status not in DIRECT_MANAGEMENT_STATUSES and not sample_return_to_diagnosis:
        result.blockers.append(Blocker(
            "order_phase",
            f"The order is in {order.status.value}; direct changes are only allowed from Reception through Diagnosis",
        ))

    def waived(report_id: Optional[str]) -> bool:
        """Is review evidence attributable to a retracted report (or, when every report is retracted and
        the row names none, to those reports)?"""
        if not retracted_ids:
            return False
        if report_id is None:
            return not has_other_report
        return report_id in retracted_ids

    for report in reports:
        rid = str(report.id)
        versions = session.exec(
            select(ReportVersion).where(ReportVersion.report_id == report.id).execution_options(**opts)
        ).all()
        if rid in retracted_ids:
            if any(v.publish_started_at is not None or v.pdf_generation_status == "GENERATING" for v in versions):
                result.blockers.append(Blocker(
                    "report_operation_active", "A signing, publication or PDF operation is still active", rid))
            if session.exec(
                select(ReportAmendment.id).where(
                    ReportAmendment.report_id == report.id, ReportAmendment.state == AMENDMENT_STATE_OPEN
                ).limit(1).execution_options(**opts)
            ).first() is not None:
                result.blockers.append(Blocker("report_amendment_open", "The report has an open amendment", rid))
            result.retraction_exception_report_ids.append(rid)
            continue
        if report.status != ReportStatus.DRAFT:
            result.blockers.append(Blocker(
                "report_not_draft", f"A related report is {report.status.value}, not a draft", rid))
        if report.published_at is not None:
            result.blockers.append(Blocker("report_published", "A related report was published", rid))
        if any(
            v.signed_at is not None or v.signed_by is not None or v.pdf_storage_id is not None
            or v.pdf_sha256 is not None or v.publish_started_at is not None or v.pdf_generation_status == "GENERATING"
            for v in versions
        ):
            result.blockers.append(Blocker(
                "report_signing_evidence",
                "A related report version has signing, publication or issued-PDF evidence", rid))

    decided = session.exec(
        select(ReportReview.report_id, ReportReview.status).where(
            ReportReview.order_id == order.id,
            (ReportReview.status != ReviewStatus.PENDING) | ReportReview.decision_at.is_not(None),
        ).execution_options(**opts)
    ).all()
    if any(
        not waived(str(rid) if rid else None)
        and not (status == ReviewStatus.REJECTED and (
            str(rid) in returned_drafts if rid else len(reports) == 1 and bool(returned_drafts)
        ))
        for rid, status in decided
    ):
        result.blockers.append(Blocker(
            "report_review_evidence", "A reviewer already recorded a decision on a related report"))
    if any(
        event_type in REVIEW_EVIDENCE_EVENTS
        and not waived(_event_report_id(metadata))
        and not (
            event_type in (EventType.REPORT_SUBMITTED, EventType.REPORT_CHANGES_REQUESTED)
            and _event_report_id(metadata) in returned_drafts
        )
        for event_type, metadata in events
    ):
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


def exception_metadata(eligibility: Eligibility) -> dict:
    """Audit metadata naming the retracted reports whose evidence a SAMPLE-scope action ignored."""
    ids = eligibility.retraction_exception_report_ids
    return {"retraction_exception_report_ids": list(ids)} if ids else {}


def evaluate(session: Session, order: Order, scope: str = SCOPE_ORDER) -> Eligibility:
    """Non-locking evaluation, for the "what can I do" read model. Never a substitute for
    `lock_and_guard` before a write."""
    return _inspect(session, order, scope)


def lock_and_guard(session: Session, order_id, tenant_id, scope: str = SCOPE_ORDER) -> Order:
    """`lock_and_guard_with_eligibility` for callers that only need the order."""
    return lock_and_guard_with_eligibility(session, order_id, tenant_id, scope)[0]


def lock_and_guard_with_eligibility(session: Session, order_id, tenant_id, scope: str = SCOPE_ORDER):
    """Lock the order, then check the whole policy against the locked state.

    Returns the freshly read `Order` and its `Eligibility`. Raises 404 for an unknown/foreign/removed order and
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
    eligibility = _inspect(session, order, scope)
    if not eligibility.allowed:
        raise LifecycleError(eligibility.blockers)
    return order, eligibility


def lock_order_for_billing(session: Session, order_id, tenant_id, *, not_found: str = "Invoice not found") -> Order:
    """Lock protocol shared by removal and EVERY billing writer (payment, invoice creation, invoice items).

    Takes the order lifecycle lock first, then re-reads the order fresh (bypassing the identity map and
    the removed-record filter) and refuses a foreign or REMOVED order with a 404: the invoice of a
    removed capture is hidden from billing, so no payment or item may commit against it. Because removal
    reads payment evidence under this same lock, exactly one of {payment commits, removal commits} wins:
    a payment already in flight makes removal wait and then refuse (`invoice_has_payments`); a removal
    already in flight makes the payment wait and then 404. Lock order is lifecycle lock -> invoice/payment
    rows, never the reverse, so it cannot deadlock with removal, report submission or sample creation.
    """
    lock_order(session, order_id)
    order = session.exec(
        select(Order).where(Order.id == order_id, Order.tenant_id == tenant_id)
        .execution_options(populate_existing=True, **{INCLUDE_REMOVED: True})
    ).first()
    if order is None or order.removed_at is not None:
        raise HTTPException(404, not_found)
    return order


def sample_creation_blockers(session: Session, order: Order) -> List[Blocker]:
    """Céluma 1.4.4: a sample cannot be ADDED to an existing order while the order or one of its reports is
    in active review (`REVIEW` status, or a report `IN_REVIEW`/`APPROVED`, which is review plus the signing
    step). Reads fresh rows; call it under the order lock (`lock_and_refresh_order`), so a concurrent
    submission either lands first (and this refuses) or waits for this transaction (and then sees the sample)."""
    opts = {INCLUDE_REMOVED: True, "populate_existing": True}
    blockers: List[Blocker] = []
    if order.status == OrderStatus.REVIEW:
        blockers.append(Blocker(ORDER_IN_REVIEW, "The order is in review; samples cannot be added until the review ends"))
    reports = session.exec(
        select(Report).where(Report.order_id == order.id, Report.status.in_(ACTIVE_REVIEW_REPORT_STATUSES))
        .order_by(Report.id).execution_options(**opts)
    ).all()
    for report in reports:
        blockers.append(Blocker(
            ORDER_IN_REVIEW,
            f"A related report is {report.status.value}; samples cannot be added while it is under review",
            str(report.id)))
    return blockers


def guard_sample_creation(session: Session, order: Order) -> None:
    """Raise 409 `order_in_review` (with every blocker) when a sample cannot be added now."""
    blockers = sample_creation_blockers(session, order)
    if blockers:
        raise HTTPException(409, {
            "code": ORDER_IN_REVIEW,
            "message": "Samples cannot be added while the order or its report is in review: "
                       + "; ".join(b.message for b in blockers),
            "blockers": [b.as_dict() for b in blockers],
        })
