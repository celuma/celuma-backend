"""Auditable operational removal of erroneous captures (Céluma 1.4, issue #11).

Engineering close-out decision (not a personal approval by the owner): removal is RETAINED
OPERATIONAL REMOVAL, a tombstone and never a physical delete. The row keeps its id, code, ledger entry,
relationships, storage objects and audit trail, indefinitely for this release; every ordinary query
stops seeing it (`app/core/soft_removal.py`). There is NO restore action and NO automatic purge in this
block (both deferred); retained data merely keeps them possible later. Consequences:

- Window and evidence: identical to every direct edit (`order_lifecycle.lock_and_guard`): Reception
  through Diagnosis, and no review/approval/signing/publication evidence on ANY related report or
  version, historical included. A report returned from review is NOT removable.
- Draft reports: a DRAFT report with no evidence is retained and hidden together with its order.
- Invoice / payments: the invoice created with the order is retained. Removal is BLOCKED when the
  invoice has any payment or is not PENDING (read under the order lock that every billing writer also
  takes, see `order_lifecycle.lock_order_for_billing`, so a payment and a removal cannot both commit): nothing is refunded, voided or deleted silently; the
  billing flow must resolve it first. A PENDING invoice without payments stays in the database and
  disappears from billing lists with its order.
- Samples of a removed order are removed with it (their images stay in storage, still counted by usage).
- Last sample: removing an order's only remaining sample is BLOCKED (`last_sample`); the order is
  never removed automatically. Remove the order, or register the replacement sample first.
- Numbering: codes stay occupied (raw-SQL availability checks and the ledger see tombstones), so a
  removed code is never reissued and no other order is renumbered. An idempotency receipt of the
  removed creation refuses a replay (410) instead of recreating it.
- Retry: removing an already removed record answers 200 `already_removed` (a lost response is safe).
- Assignments, labels, comments, events and notifications are retained untouched; their links no
  longer resolve (404) because the target is hidden.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List

from fastapi import HTTPException
from sqlmodel import Session, select

from app.core.soft_removal import INCLUDE_REMOVED
from app.models.billing import Invoice, Payment
from app.models.enums import EventType, PaymentStatus
from app.models.events import OrderEvent
from app.models.laboratory import Order, Sample
from app.models.report import Report
from app.services import order_lifecycle
from app.services.order_lifecycle import Blocker, LifecycleError
from app.services.sample_code_correction import clean_reason
from app.services.sample_types import record_audit


@dataclass
class RemovalResult:
    """Plain values only: the removed row is hidden from the ORM after commit, so the response is
    built from what was captured while it was still loaded."""
    kind: str
    entity_id: str
    code: str
    already_removed: bool
    removed_at: datetime | None
    reason: str | None
    order_id: str
    order_code: str
    samples_removed: int = 0
    reports_hidden: int = 0
    invoice_retained: bool = False


def invoice_blockers(session: Session, order: Order) -> List[Blocker]:
    """Financial dependencies that no removal may touch."""
    invoice = session.exec(select(Invoice).where(Invoice.order_id == order.id)).first()
    if invoice is None:
        return []
    out: List[Blocker] = []
    payments = session.exec(select(Payment.id).where(Payment.invoice_id == invoice.id).limit(1)).first()
    if payments is not None or (invoice.amount_paid or 0) > 0:
        out.append(Blocker(
            "invoice_has_payments",
            "The order's invoice has payments; resolve them through billing before removing the order"))
    if invoice.status != PaymentStatus.PENDING:
        out.append(Blocker(
            "invoice_not_pending",
            f"The order's invoice is {invoice.status.value}; resolve it through billing before removing the order"))
    return out


def sample_blockers(session: Session, sample: Sample) -> List[Blocker]:
    live = session.exec(
        select(Sample.id).where(Sample.order_id == sample.order_id, Sample.id != sample.id).limit(1)
    ).first()
    if live is None:
        return [Blocker(
            "last_sample",
            "This is the order's only sample; the order is never removed automatically. Remove the order, "
            "or register the replacement sample first")]
    return []


def _find(session: Session, model, entity_id, tenant_id):
    return session.exec(
        select(model).where(model.id == entity_id, model.tenant_id == tenant_id)
        .execution_options(populate_existing=True, **{INCLUDE_REMOVED: True})
    ).first()


def remove_order(session: Session, *, order_id, tenant_id, user, reason: str) -> RemovalResult:
    reason = clean_reason(reason)
    probe = _find(session, Order, order_id, tenant_id)
    if probe is None:
        raise HTTPException(404, "Order not found")
    order_lifecycle.lock_order(session, order_id)
    order = _find(session, Order, order_id, tenant_id)
    if order.removed_at is not None:
        result = RemovalResult("order", str(order.id), order.order_code, True, order.removed_at,
                               order.removal_reason, str(order.id), order.order_code)
        session.rollback()
        return result

    eligibility = order_lifecycle.evaluate(session, order)
    blockers = list(eligibility.blockers) + invoice_blockers(session, order)
    if blockers:
        raise LifecycleError(blockers, "This order cannot be removed: " + "; ".join(b.message for b in blockers))

    now = datetime.utcnow()
    invoice = session.exec(select(Invoice).where(Invoice.order_id == order.id)).first()  # before the tombstone hides it
    samples = session.exec(
        select(Sample).where(Sample.order_id == order.id).execution_options(populate_existing=True)).all()
    for sample in samples:
        sample.removed_at, sample.removed_by, sample.removal_reason = now, user.id, reason
        session.add(sample)
    reports = session.exec(
        select(Report).where(Report.order_id == order.id).execution_options(populate_existing=True)).all()
    for report in reports:
        report.removed_at = now
        session.add(report)
    order.removed_at, order.removed_by, order.removal_reason = now, user.id, reason
    session.add(order)
    session.add(OrderEvent(
        tenant_id=order.tenant_id, branch_id=order.branch_id, order_id=order.id, sample_id=None,
        event_type=EventType.ORDER_REMOVED, description="",
        event_metadata={"order_id": str(order.id), "order_code": order.order_code, "reason": reason,
                        "samples_removed": len(samples), "reports_hidden": len(reports)},
        created_by=user.id,
    ))
    record_audit(
        session, tenant_id=order.tenant_id, user_id=user.id, action="ORDER.REMOVED", entity_type="order",
        entity_id=order.id, description="Order removed as an erroneous capture",
        metadata={"reason": reason, "order_code": order.order_code,
                  "sample_codes": [s.sample_code for s in samples],
                  "report_ids": [str(r.id) for r in reports],
                  "invoice_id": str(invoice.id) if invoice else None},
    )
    result = RemovalResult("order", str(order.id), order.order_code, False, now, reason, str(order.id),
                           order.order_code, samples_removed=len(samples), reports_hidden=len(reports),
                           invoice_retained=invoice is not None)
    session.commit()
    return result


def remove_sample(session: Session, *, sample_id, tenant_id, user, reason: str) -> RemovalResult:
    reason = clean_reason(reason)
    probe = _find(session, Sample, sample_id, tenant_id)
    if probe is None:
        raise HTTPException(404, "Sample not found")
    order_lifecycle.lock_order(session, probe.order_id)
    sample = _find(session, Sample, sample_id, tenant_id)
    order = _find(session, Order, sample.order_id, tenant_id)
    if sample.removed_at is not None:
        result = RemovalResult("sample", str(sample.id), sample.sample_code, True, sample.removed_at,
                               sample.removal_reason, str(order.id), order.order_code)
        session.rollback()
        return result

    eligibility = order_lifecycle.evaluate(session, order)
    blockers = list(eligibility.blockers) + sample_blockers(session, sample)
    if blockers:
        raise LifecycleError(blockers, "This sample cannot be removed: " + "; ".join(b.message for b in blockers))

    removed_at = datetime.utcnow()
    sample.removed_at, sample.removed_by, sample.removal_reason = removed_at, user.id, reason
    session.add(sample)
    session.flush()
    session.add(OrderEvent(
        tenant_id=sample.tenant_id, branch_id=sample.branch_id, order_id=sample.order_id, sample_id=sample.id,
        event_type=EventType.SAMPLE_REMOVED, description="",
        event_metadata={"sample_id": str(sample.id), "sample_code": sample.sample_code, "reason": reason,
                        "order_code": order.order_code},
        created_by=user.id,
    ))
    record_audit(
        session, tenant_id=sample.tenant_id, user_id=user.id, action="SAMPLE.REMOVED", entity_type="sample",
        entity_id=sample.id, description="Sample removed as an erroneous capture",
        metadata={"reason": reason, "sample_code": sample.sample_code, "order_id": str(sample.order_id)},
    )
    # The order's aggregate status follows its remaining samples (same rule as every sample change).
    from app.api.v1.laboratory import update_order_status
    update_order_status(str(sample.order_id), session)
    result = RemovalResult("sample", str(sample.id), sample.sample_code, False, removed_at, reason,
                           str(order.id), order.order_code)
    session.commit()
    return result
