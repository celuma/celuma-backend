"""Withdrawing a report from review (Céluma 1.4.4, issue #73): IN_REVIEW -> DRAFT by whoever submitted it.

The author of the CURRENT submission may pull a still-undecided review back to an editable draft, correct it
and resubmit without waiting for a reviewer. It is NOT a reviewer decision (no reviewer role, no assignment) and
it is NOT an administrative power: another submit-capable user can never cancel somebody else's request.

Authority (all enforced by the route, re-checked under the lock):
  * capability `reports:submit` and tenant scope;
  * the recorded actor of the current submission: the latest `REPORT_SUBMITTED` timeline event of this report
    (`submitted_by`, `report_id`). A report with no such record cannot be withdrawn (unknown actor = no authority);
  * the report is `IN_REVIEW` and no reviewer decision exists on the order's current cycle;
  * the request names the submission it saw (`submission_id` = that event's id): a stale request that targets an
    older submission is refused with 409 and the UI refreshes.

Effects: status back to DRAFT through the normal order-status workflow, one audit row (`REPORT.WITHDRAW_REVIEW`) and
one timeline event (`STATUS_CHANGED`, `metadata.action = REPORT_REVIEW_WITHDRAWN`, the precedent of the reopen
event: no new `eventtype` value, so no migration). Versions, content, reviewer assignments, earlier audit/timeline
rows and every issued artifact are untouched. During an open amendment the amendment stays open and the delivered
emission is unchanged (the report is DRAFT/IN_REVIEW/APPROVED for the whole amendment anyway).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlmodel import Session, select

from app.core.soft_removal import INCLUDE_REMOVED
from app.models.enums import EventType, ReportStatus, ReviewStatus
from app.models.events import OrderEvent
from app.models.report import Report
from app.models.report_review import ReportReview

ACTION_REVIEW_WITHDRAWN = "REPORT_REVIEW_WITHDRAWN"
AUDIT_WITHDRAW_REVIEW = "REPORT.WITHDRAW_REVIEW"

NOT_PENDING = "review_withdraw_not_pending"
DECISION_RECORDED = "review_decision_recorded"
SUBMISSION_STALE = "review_submission_stale"
NOT_SUBMITTER = "review_withdraw_not_submitter"
SUBMISSION_UNKNOWN = "review_submission_unknown"


@dataclass
class Withdrawal:
    allowed: bool
    submission_id: Optional[str] = None
    code: Optional[str] = None
    message: Optional[str] = None


def current_submission(session: Session, report: Report) -> Optional[OrderEvent]:
    """The latest `REPORT_SUBMITTED` event naming this report (the submission that put it in its current review)."""
    events = session.exec(
        select(OrderEvent).where(OrderEvent.order_id == report.order_id, OrderEvent.event_type == EventType.REPORT_SUBMITTED)
        .order_by(OrderEvent.created_at.desc(), OrderEvent.id.desc()).execution_options(populate_existing=True, **{INCLUDE_REMOVED: True})
    ).all()
    for event in events:
        if (event.event_metadata or {}).get("report_id") == str(report.id):
            return event
    return None


def _decided(session: Session, report: Report) -> bool:
    return session.exec(
        select(ReportReview.id).where(
            ReportReview.order_id == report.order_id,
            (ReportReview.status != ReviewStatus.PENDING) | ReportReview.decision_at.is_not(None),
        ).limit(1).execution_options(populate_existing=True)
    ).first() is not None


def evaluate(session: Session, report: Report, user_id, *, permitted: bool, submission_id: Optional[str] = None) -> Withdrawal:
    """Decide on FRESH rows (the caller holds the order lock for a write). `submission_id`, when given, must be the
    current submission."""
    if not permitted:
        return Withdrawal(False, code="permission", message="Permission required: reports:submit")
    if report.status != ReportStatus.IN_REVIEW:
        return Withdrawal(False, code=NOT_PENDING,
                          message=f"The report is {getattr(report.status, 'value', report.status)}, not in review")
    submission = current_submission(session, report)
    if submission is None:
        return Withdrawal(False, code=SUBMISSION_UNKNOWN, message="The submission of this review is not recorded")
    sid = str(submission.id)
    if (submission.event_metadata or {}).get("submitted_by") != str(user_id):
        return Withdrawal(False, sid, NOT_SUBMITTER, "Only the person who submitted the report can withdraw it from review")
    if _decided(session, report):
        return Withdrawal(False, sid, DECISION_RECORDED, "A reviewer already recorded a decision")
    if submission_id is not None and submission_id != sid:
        return Withdrawal(False, sid, SUBMISSION_STALE, "The review was submitted again; refresh and try again")
    return Withdrawal(True, sid)
