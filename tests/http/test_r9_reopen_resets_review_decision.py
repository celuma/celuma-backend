"""Céluma 1.3.1 manual-validation remediation — R9 (CEL-131-03).

**The finding.** After `APPROVED -> reopen -> DRAFT` the reviewer was still
rendered with the green APPROVED check. The report no longer carries an
approval, so displaying one is wrong — and it is wrong in the direction that
matters, because the next thing a user does with a reopened report is decide
whether it still needs reviewing.

**Why the existing suite allowed it through.** Block B preserved the decision
DELIBERATELY and documented the reasoning
(`lifecycle-contract.md` §6): `submit_report` already resets every reviewer to
PENDING on the next submission, so the stale value was thought to be harmless
in the meantime, and `report_review` keeps only the latest decision per
reviewer, so clearing it looked like destroying evidence. Both premises were
examined again here:

* The window is NOT harmless. Between the reopen and the next submission the
  UI states that the report currently holds an approval it does not.
* No evidence is destroyed. `report_review` was never the historical record —
  it only ever held the LATEST decision, which is why the durable record lives
  in `audit_log` (`REPORT.APPROVE`) and the order timeline
  (`REPORT_APPROVED`). Both are untouched, and this module asserts that.

**The transition, precisely:** the assignment is preserved, the current
decision is cleared to PENDING (never REJECTED — the previous cycle was not
rejected, it merely stopped being current), and every reviewer participating in
the order's cycle is reset rather than only whoever happened to act.
"""
from datetime import datetime

import pytest
from sqlmodel import Session, select

from app.models.audit import AuditLog
from app.models.enums import EventType, ReportStatus, ReviewStatus
from app.models.events import OrderEvent
from app.models.report import Report, ReportVersion
from app.models.report_review import ReportReview

from .factories import (
    assign_reviewer,
    auth_headers,
    create_branch,
    create_order,
    create_report,
    create_tenant,
    create_user,
)

REOPEN = "/api/v1/reports/{}/reopen"
SUBMIT = "/api/v1/reports/{}/submit"
APPROVE = "/api/v1/reports/{}/approve"


def _reviews(session: Session, order_id):
    session.expire_all()
    return session.exec(
        select(ReportReview)
        .where(ReportReview.order_id == order_id)
        .order_by(ReportReview.assigned_at)
    ).all()


def _reopen_events(session: Session, order_id):
    session.expire_all()
    return [
        e
        for e in session.exec(
            select(OrderEvent)
            .where(OrderEvent.order_id == order_id)
            .order_by(OrderEvent.created_at)
        ).all()
        if (e.event_metadata or {}).get("action") == "REPORT_REOPENED"
    ]


@pytest.fixture
def lab(session: Session):
    """An APPROVED + UNSIGNED report whose assigned reviewer has already
    decided — the state a real report is in when someone reopens it."""
    tenant = create_tenant(session)
    branch = create_branch(session, tenant)
    order = create_order(session, tenant, branch)
    report, version = create_report(
        session, tenant, branch, order, status=ReportStatus.APPROVED
    )
    reviewer = create_user(session, tenant, email="rev@t1.example", roles=("reviewer",))
    review = assign_reviewer(
        session, order, reviewer, report=report, status=ReviewStatus.APPROVED
    )
    review.decision_at = datetime.utcnow()
    session.add(review)
    session.commit()
    return {
        "tenant": tenant, "branch": branch, "order": order,
        "report": report, "version": version, "reviewer": reviewer,
        "review_id": review.id,
    }


class TestTheDecisionIsReset:
    def test_an_approved_reviewer_becomes_pending(self, client, session, lab):
        before = _reviews(session, lab["order"].id)
        assert [r.status for r in before] == [ReviewStatus.APPROVED]

        resp = client.post(
            REOPEN.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["reviewer"]),
        )
        assert resp.status_code == 200, resp.text

        rows = _reviews(session, lab["order"].id)
        assert [r.status for r in rows] == [ReviewStatus.PENDING]
        assert rows[0].decision_at is None

    def test_it_is_never_marked_rejected(self, client, session, lab):
        """REJECTED means a reviewer asked for changes. A reopen is not that,
        and recording it as such would misstate what happened."""
        client.post(
            REOPEN.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["reviewer"]),
        )
        rows = _reviews(session, lab["order"].id)
        assert ReviewStatus.REJECTED not in {r.status for r in rows}

    def test_the_assignment_itself_is_preserved(self, client, session, lab):
        client.post(
            REOPEN.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["reviewer"]),
        )

        rows = _reviews(session, lab["order"].id)
        assert len(rows) == 1, "reopening must not create or delete an assignment"
        assert rows[0].id == lab["review_id"], "the row was replaced, not reset"
        assert rows[0].reviewer_user_id == lab["reviewer"].id
        assert rows[0].order_id == lab["order"].id

    def test_the_reviewer_is_still_visible_through_the_read_model(
        self, client, lab
    ):
        """Assigned but pending — never absent, and never still approved."""
        client.post(
            REOPEN.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["reviewer"]),
        )

        resp = client.get(
            f"/api/v1/laboratory/orders/{lab['order'].id}/full",
            headers=auth_headers(lab["reviewer"]),
        )
        assert resp.status_code == 200, resp.text
        reviewers = resp.json()["order"]["reviewers"]
        assert len(reviewers) == 1
        assert reviewers[0]["id"] == str(lab["reviewer"].id)
        assert reviewers[0]["status"] == "pending"

    def test_every_reviewer_of_the_order_is_reset_not_just_the_actor(
        self, client, session, lab
    ):
        """A second reviewer who also approved must be reconsidered too — the
        reopened report is not approved by anyone any more."""
        second = create_user(
            session, lab["tenant"], email="rev2@t1.example", roles=("reviewer",)
        )
        other = assign_reviewer(
            session, lab["order"], second, report=lab["report"],
            status=ReviewStatus.APPROVED,
        )
        other.decision_at = datetime.utcnow()
        session.add(other)
        session.commit()

        # Reopened by an administrator, so neither row belongs to the actor.
        admin = create_user(session, lab["tenant"], email="admin@t1.example", roles=("admin",))
        resp = client.post(
            REOPEN.format(lab["report"].id), json={}, headers=auth_headers(admin)
        )
        assert resp.status_code == 200, resp.text

        rows = _reviews(session, lab["order"].id)
        assert len(rows) == 2
        assert {r.status for r in rows} == {ReviewStatus.PENDING}
        assert all(r.decision_at is None for r in rows)


class TestHistoryIsNotDestroyed:
    def test_the_approval_audit_record_survives(self, client, session, lab):
        """`report_review` holds only the CURRENT decision; the durable record
        of the previous one is its own audit row, which this never touches."""
        session.add(
            AuditLog(
                tenant_id=lab["tenant"].id,
                branch_id=lab["branch"].id,
                actor_user_id=lab["reviewer"].id,
                action="REPORT.APPROVE",
                entity_type="report",
                entity_id=lab["report"].id,
                new_values={"status": "APPROVED"},
            )
        )
        session.commit()

        client.post(
            REOPEN.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["reviewer"]),
        )

        session.expire_all()
        approvals = session.exec(
            select(AuditLog).where(
                AuditLog.entity_id == lab["report"].id,
                AuditLog.action == "REPORT.APPROVE",
            )
        ).all()
        assert len(approvals) == 1
        assert approvals[0].actor_user_id == lab["reviewer"].id

    def test_the_reopen_audit_row_records_what_it_invalidated(
        self, client, session, lab
    ):
        client.post(
            REOPEN.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["reviewer"]),
        )

        session.expire_all()
        rows = session.exec(
            select(AuditLog).where(
                AuditLog.entity_id == lab["report"].id,
                AuditLog.action == "REPORT.REOPEN",
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].old_values["status"] == "APPROVED"
        assert rows[0].new_values["status"] == "DRAFT"
        assert rows[0].new_values["reviews_reset_to_pending"] == 1

    def test_the_approval_timeline_event_survives(self, client, session, lab):
        session.add(
            OrderEvent(
                tenant_id=lab["tenant"].id,
                branch_id=lab["branch"].id,
                order_id=lab["order"].id,
                event_type=EventType.REPORT_APPROVED,
                description="",
                event_metadata={"report_id": str(lab["report"].id)},
                created_by=lab["reviewer"].id,
            )
        )
        session.commit()

        client.post(
            REOPEN.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["reviewer"]),
        )

        session.expire_all()
        approved = session.exec(
            select(OrderEvent).where(
                OrderEvent.order_id == lab["order"].id,
                OrderEvent.event_type == EventType.REPORT_APPROVED,
            )
        ).all()
        assert len(approved) == 1

    def test_the_reopen_timeline_event_is_still_written_exactly_once(
        self, client, session, lab
    ):
        client.post(
            REOPEN.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["reviewer"]),
        )
        assert len(_reopen_events(session, lab["order"].id)) == 1


class TestNothingChangesWhenTheReopenFails:
    def test_an_unauthorized_reopen_leaves_every_decision_intact(
        self, client, session, lab
    ):
        patho = create_user(
            session, lab["tenant"], email="path@t1.example", roles=("pathologist",)
        )

        resp = client.post(
            REOPEN.format(lab["report"].id), json={}, headers=auth_headers(patho)
        )
        assert resp.status_code == 403, resp.text

        rows = _reviews(session, lab["order"].id)
        assert [r.status for r in rows] == [ReviewStatus.APPROVED]
        assert rows[0].decision_at is not None
        assert _reopen_events(session, lab["order"].id) == []

    def test_a_reopen_refused_on_state_leaves_every_decision_intact(
        self, client, session, lab
    ):
        report = session.get(Report, lab["report"].id)
        report.status = ReportStatus.PUBLISHED
        session.add(report)
        session.commit()

        resp = client.post(
            REOPEN.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["reviewer"]),
        )
        assert resp.status_code == 400, resp.text
        assert [r.status for r in _reviews(session, lab["order"].id)] == [
            ReviewStatus.APPROVED
        ]

    def test_a_reopen_refused_because_the_report_was_signed_changes_nothing(
        self, client, session, lab
    ):
        version = session.get(ReportVersion, lab["version"].id)
        version.signed_by = lab["reviewer"].id
        version.signed_at = datetime.utcnow()
        session.add(version)
        session.commit()

        resp = client.post(
            REOPEN.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["reviewer"]),
        )
        assert resp.status_code == 409, resp.text
        assert [r.status for r in _reviews(session, lab["order"].id)] == [
            ReviewStatus.APPROVED
        ]


class TestTheNewCycleBehavesLikeAnyOther:
    def test_resubmission_reuses_the_assignment_and_adds_no_duplicate(
        self, client, session, lab
    ):
        client.post(
            REOPEN.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["reviewer"]),
        )
        author = create_user(
            session, lab["tenant"], email="author@t1.example", roles=("pathologist",)
        )

        resp = client.post(
            SUBMIT.format(lab["report"].id), json={}, headers=auth_headers(author)
        )
        assert resp.status_code == 200, resp.text

        rows = _reviews(session, lab["order"].id)
        assert len(rows) == 1
        assert rows[0].id == lab["review_id"]
        assert rows[0].status == ReviewStatus.PENDING

    def test_the_report_must_be_approved_again_before_it_is_approved_again(
        self, client, session, lab
    ):
        """The point of resetting: the reopened report genuinely re-enters the
        cycle, and only a fresh decision can take it back to APPROVED."""
        client.post(
            REOPEN.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["reviewer"]),
        )
        session.expire_all()
        assert session.get(Report, lab["report"].id).status == ReportStatus.DRAFT

        # Approval is refused from DRAFT — the report has to be submitted.
        refused = client.post(
            APPROVE.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["reviewer"]),
        )
        assert refused.status_code == 400, refused.text

        author = create_user(
            session, lab["tenant"], email="author@t1.example", roles=("pathologist",)
        )
        client.post(SUBMIT.format(lab["report"].id), json={}, headers=auth_headers(author))

        approved = client.post(
            APPROVE.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["reviewer"]),
        )
        assert approved.status_code == 200, approved.text

        session.expire_all()
        assert session.get(Report, lab["report"].id).status == ReportStatus.APPROVED
        rows = _reviews(session, lab["order"].id)
        assert [r.status for r in rows] == [ReviewStatus.APPROVED]
        assert rows[0].decision_at is not None

    def test_repeated_reopen_cycles_never_accumulate_rows(
        self, client, session, lab
    ):
        author = create_user(
            session, lab["tenant"], email="author@t1.example", roles=("pathologist",)
        )
        for _ in range(3):
            client.post(
                REOPEN.format(lab["report"].id),
                json={},
                headers=auth_headers(lab["reviewer"]),
            )
            client.post(SUBMIT.format(lab["report"].id), json={}, headers=auth_headers(author))
            client.post(
                APPROVE.format(lab["report"].id),
                json={},
                headers=auth_headers(lab["reviewer"]),
            )

        rows = _reviews(session, lab["order"].id)
        assert len(rows) == 1
        assert len(_reopen_events(session, lab["order"].id)) == 3
