"""Samples become Ready when their order enters report review (Céluma issue #86, PROPOSED pending Rafael's review).

A successful `POST /reports/{id}/submit` moves the report DRAFT -> IN_REVIEW and, through the derived order phase,
the order to REVIEW. In that SAME transaction, after every submission guard has passed and only when the
recalculated order phase is actually `REVIEW`, the order's eligible samples are moved to `READY` ("Lista").

Proposed rule (conservative first pass, see `celuma-engineering/adr/0024-samples-ready-on-review.md`):

  * Scope: active (not tombstoned) samples of the submitted report's own order and tenant. Nothing else is read
    or written.
  * Source states: `RECEIVED` and `PROCESSING` only.
  * Preserved: `READY` (no write, no event), `DAMAGED` ("Insuficiente"), `CANCELLED` and removed ("retirada")
    samples. An exceptional state is a clinical judgement that an automatic transition must never overwrite.
  * The same rule applies to every successful submission: first submission, resubmission after a return to draft
    and the submission of an open amendment. Returning to draft never reverts a sample to its earlier state.

Concurrency: the caller holds the order lifecycle lock (`order_lifecycle.lock_report_order`), so two submissions
of the same order are serialised and the loser sees `IN_REVIEW` and is refused before reaching this code. The
manual `PATCH /laboratory/samples/{id}/state` route does not take the order lock, so the eligible rows are
re-read and row-locked here (`FOR UPDATE`, lock order: order lock -> sample rows, never the reverse) and the state
filter is re-evaluated by PostgreSQL after any wait: a manual Insuficiente/Cancelada committed concurrently is
never overwritten by a stale read.

Audit: one `SAMPLE_STATE_CHANGED` timeline row per changed sample (the existing event type, `trigger =
report_submitted`, correlated to the submission event), plus the list of changed samples on the existing
`REPORT.SUBMIT` audit row. No new event type, no audit action, no migration. Notifications reuse the existing
in-app-only `SAMPLE_STATUS_CHANGED` integration, exactly like the automatic first-image transition.
"""
from __future__ import annotations

from typing import List

from sqlmodel import Session, select

from app.models.enums import EventType, OrderStatus, SampleState
from app.models.events import OrderEvent
from app.models.laboratory import Order, Sample
from app.models.report import Report
from app.models.user import AppUser
from app.services.notification_integrations import notify_sample_status_changed
from app.services.sample_types import ref_for_sample

TRIGGER_REPORT_SUBMITTED = "report_submitted"

#: Proposed source states (#86). Everything else is preserved as it is.
READY_ON_REVIEW_SOURCE_STATES = (SampleState.RECEIVED, SampleState.PROCESSING)


def mark_samples_ready_on_review(
    session: Session,
    *,
    report: Report,
    order: Order | None,
    actor: AppUser,
    submission_event: OrderEvent,
    amendment_meta: dict | None = None,
) -> List[dict]:
    """Move the order's eligible samples to READY. Call under the order lock, after the report is IN_REVIEW and the
    order phase was recalculated. Returns one summary per changed sample (for the submit audit row); an empty list
    when nothing was eligible or the order did not enter REVIEW (for example a CANCELLED order, which is never
    recalculated). Writes nothing on its own commit: it shares the submission transaction, so a later failure rolls
    the sample states, events and notifications back together with the submission."""
    if order is None or report.order_id is None:
        return []
    if order.id != report.order_id or order.tenant_id != report.tenant_id:
        return []
    if order.status != OrderStatus.REVIEW:
        return []

    samples = session.exec(
        select(Sample)
        .where(
            Sample.order_id == report.order_id,
            Sample.tenant_id == report.tenant_id,
            Sample.removed_at.is_(None),
            Sample.state.in_(READY_ON_REVIEW_SOURCE_STATES),
        )
        .order_by(Sample.id)
        .with_for_update(of=Sample)
        .execution_options(populate_existing=True)
    ).all()

    changed: List[dict] = []
    for sample in samples:
        # Defensive re-check on the freshly locked row (populate_existing): never overwrite anything else.
        if sample.removed_at is not None or sample.state not in READY_ON_REVIEW_SOURCE_STATES:
            continue
        old_state = sample.state.value
        sample.state = SampleState.READY
        session.add(sample)

        type_ref = ref_for_sample(session, sample)
        event = OrderEvent(
            tenant_id=sample.tenant_id,
            branch_id=sample.branch_id,
            order_id=sample.order_id,
            sample_id=sample.id,
            event_type=EventType.SAMPLE_STATE_CHANGED,
            description="",  # Not used - message built in UI from metadata
            event_metadata={
                "old_state": old_state,
                "new_state": SampleState.READY.value,
                "sample_id": str(sample.id),
                "sample_code": sample.sample_code,
                "sample_type": type_ref.name if type_ref else None,
                "sample_type_id": type_ref.id if type_ref else None,
                "trigger": TRIGGER_REPORT_SUBMITTED,
                "report_id": str(report.id),
                "submission_id": str(submission_event.id),
                **(amendment_meta or {}),
            },
            created_by=actor.id,
        )
        session.add(event)
        notify_sample_status_changed(
            session,
            sample=sample,
            order=order,
            old_state=old_state,
            new_state=SampleState.READY.value,
            actor=actor,
            occurrence_marker=str(event.id),
        )
        changed.append({"sample_id": str(sample.id), "sample_code": sample.sample_code, "old_state": old_state})
    return changed
