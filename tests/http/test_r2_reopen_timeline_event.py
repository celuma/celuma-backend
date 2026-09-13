"""Céluma 1.3.1 manual-validation remediation — R2 (CEL-131-03).

**The finding.** Reopening an APPROVED + UNSIGNED report worked, but nothing
appeared in the user-visible timeline. Every other report transition —
created, edited, submitted, approved, changes requested, signed, retracted —
writes an `OrderEvent` that the order's "Línea de Tiempo" renders. Reopening,
uniquely, wrote only an `audit_log` row.

**Why the existing suite allowed it through.** Block B asserted the audit
record and nothing else, because the missing timeline event was a KNOWN,
DOCUMENTED gap, not an oversight: `OrderEvent.event_type` is a native Postgres
enum with no `REPORT_REOPENED` member, and the block deliberately declined to
`ALTER TYPE … ADD VALUE` (Postgres has no DROP VALUE, so the label would
outlive any downgrade). The tests agreed with the code; the workflow did not.

**The resolution, and what it costs.** The release owner's decision is to
reuse the enum's existing generic member rather than change the enum. That
makes the stored `event_type` uninformative on its own, so the metadata — not
the type — is what identifies a reopen. These tests therefore assert the
`action` discriminator as hard as they assert the event's existence: it is
the only thing standing between "a reopen" and "some other generic status
change" for every future consumer.
"""
from datetime import datetime

import pytest
from sqlmodel import Session, select

from app.models.audit import AuditLog
from app.models.enums import EventType, ReportStatus, ReviewStatus
from app.models.events import OrderEvent
from app.models.report import Report, ReportVersion

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


def _events(session: Session, order_id):
    return session.exec(
        select(OrderEvent)
        .where(OrderEvent.order_id == order_id)
        .order_by(OrderEvent.created_at)
    ).all()


def _reopen_events(session: Session, order_id):
    """The reopen events, found the way any consumer must find them: by the
    `action` discriminator, never by the (generic) event type."""
    return [
        e
        for e in _events(session, order_id)
        if (e.event_metadata or {}).get("action") == "REPORT_REOPENED"
    ]


@pytest.fixture
def lab(session: Session):
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
        "tenant": tenant,
        "branch": branch,
        "order": order,
        "report": report,
        "version": version,
        "reviewer": reviewer,
    }


def _approve_again(session: Session, lab):
    """Return the report to APPROVED so it can be reopened a second time,
    without going through submit/approve (which would add its own events and
    make the chronology assertions about something else)."""
    report = session.get(Report, lab["report"].id)
    report.status = ReportStatus.APPROVED
    session.add(report)
    session.commit()


class TestTheReopenEventExists:
    def test_a_successful_reopen_writes_exactly_one_timeline_event(
        self, client, session, lab
    ):
        before = len(_events(session, lab["order"].id))

        resp = client.post(
            REOPEN.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["reviewer"]),
        )
        assert resp.status_code == 200, resp.text

        session.expire_all()
        after = _events(session, lab["order"].id)
        assert len(after) == before + 1, "reopen must add exactly one timeline event"
        assert len(_reopen_events(session, lab["order"].id)) == 1

    def test_the_event_is_identifiable_and_carries_the_transition(
        self, client, session, lab
    ):
        client.post(
            REOPEN.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["reviewer"]),
        )
        session.expire_all()

        event = _reopen_events(session, lab["order"].id)[0]
        meta = event.event_metadata
        assert meta["action"] == "REPORT_REOPENED"
        assert meta["report_id"] == str(lab["report"].id)
        assert meta["from_status"] == "APPROVED"
        assert meta["to_status"] == "DRAFT"
        # The statuses are stored as bare values, matching `audit_log`.
        # `ReportStatus` is a `(str, Enum)` whose `str()` renders as
        # "ReportStatus.DRAFT" on Python 3.12 — a real trap, asserted here so
        # nobody reintroduces it.
        assert "ReportStatus." not in meta["from_status"]
        assert "ReportStatus." not in meta["to_status"]

    def test_the_event_records_the_right_actor_order_and_report(
        self, client, session, lab
    ):
        client.post(
            REOPEN.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["reviewer"]),
        )
        session.expire_all()

        event = _reopen_events(session, lab["order"].id)[0]
        assert event.created_by == lab["reviewer"].id
        assert event.order_id == lab["order"].id
        assert event.tenant_id == lab["tenant"].id
        assert event.branch_id == lab["branch"].id
        assert event.event_metadata["reopened_by"] == str(lab["reviewer"].id)
        assert event.created_at is not None

    def test_the_event_carries_human_readable_text(self, client, session, lab):
        """A generic `event_type` cannot be rendered from the type alone, so
        the description is the last-resort fallback for any consumer that does
        not know the `action` key."""
        client.post(
            REOPEN.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["reviewer"]),
        )
        session.expire_all()

        event = _reopen_events(session, lab["order"].id)[0]
        assert event.description == "Reporte reabierto"

    def test_the_event_uses_the_generic_type_and_did_not_change_the_enum(
        self, client, session, lab
    ):
        """The explicit record of the release owner's decision: no
        `REPORT_REOPENED` member was added to `public.eventtype`. If someone
        later adds one, this fails and forces the migration conversation
        rather than letting the enum drift."""
        client.post(
            REOPEN.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["reviewer"]),
        )
        session.expire_all()

        event = _reopen_events(session, lab["order"].id)[0]
        assert event.event_type == EventType.STATUS_CHANGED
        assert not hasattr(EventType, "REPORT_REOPENED")


class TestNothingIsWrittenWhenTheReopenFails:
    def test_an_unauthorized_reopen_writes_no_timeline_event(
        self, client, session, lab
    ):
        patho = create_user(
            session, lab["tenant"], email="path@t1.example", roles=("pathologist",)
        )
        before = len(_events(session, lab["order"].id))

        resp = client.post(
            REOPEN.format(lab["report"].id), json={}, headers=auth_headers(patho)
        )
        assert resp.status_code == 403, resp.text

        session.expire_all()
        assert len(_events(session, lab["order"].id)) == before
        assert _reopen_events(session, lab["order"].id) == []

    def test_a_reopen_refused_on_state_writes_no_timeline_event(
        self, client, session, lab
    ):
        report = session.get(Report, lab["report"].id)
        report.status = ReportStatus.DRAFT
        session.add(report)
        session.commit()
        before = len(_events(session, lab["order"].id))

        resp = client.post(
            REOPEN.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["reviewer"]),
        )
        assert resp.status_code == 400, resp.text

        session.expire_all()
        assert len(_events(session, lab["order"].id)) == before

    def test_a_reopen_refused_because_the_report_was_signed_writes_nothing(
        self, client, session, lab
    ):
        version = session.get(ReportVersion, lab["version"].id)
        version.signed_by = lab["reviewer"].id
        version.signed_at = datetime.utcnow()
        session.add(version)
        session.commit()
        before = len(_events(session, lab["order"].id))

        resp = client.post(
            REOPEN.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["reviewer"]),
        )
        assert resp.status_code == 409, resp.text

        session.expire_all()
        assert len(_events(session, lab["order"].id)) == before


class TestRepeatedReopenCycles:
    def test_each_cycle_produces_its_own_chronological_event(
        self, client, session, lab
    ):
        for _ in range(3):
            resp = client.post(
                REOPEN.format(lab["report"].id),
                json={},
                headers=auth_headers(lab["reviewer"]),
            )
            assert resp.status_code == 200, resp.text
            session.expire_all()
            _approve_again(session, lab)

        events = _reopen_events(session, lab["order"].id)
        assert len(events) == 3, "reopen cycles must not collapse into one event"
        assert len({e.id for e in events}) == 3
        timestamps = [e.created_at for e in events]
        assert timestamps == sorted(timestamps), "events must be chronological"


class TestTheAuditTrailIsUnchanged:
    """Block B's audit record is the compliance artifact. Adding a timeline
    event must not duplicate it, replace it, or alter its shape."""

    def test_one_audit_row_and_one_timeline_row_per_reopen(
        self, client, session, lab
    ):
        client.post(
            REOPEN.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["reviewer"]),
        )
        session.expire_all()

        audit = session.exec(
            select(AuditLog).where(
                AuditLog.entity_id == lab["report"].id,
                AuditLog.action == "REPORT.REOPEN",
            )
        ).all()
        assert len(audit) == 1
        assert audit[0].old_values["status"] == "APPROVED"
        assert audit[0].new_values["status"] == "DRAFT"
        assert audit[0].actor_user_id == lab["reviewer"].id
        assert len(_reopen_events(session, lab["order"].id)) == 1

    def test_the_transition_itself_still_happens(self, client, session, lab):
        client.post(
            REOPEN.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["reviewer"]),
        )
        session.expire_all()
        assert session.get(Report, lab["report"].id).status == ReportStatus.DRAFT
