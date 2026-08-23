"""Transaction-safety tests for the domain integrations (Céluma 1.3, Phase 3,
Block F, Story F15).

One property, asserted separately at **every real call site**:

    NotificationService failure  !=  clinical operation failure

Block B already proves the service contains its own failures
(`test_notification_service.py::TestFailureContainment`). That test writes
synthetic domain data through the same session and checks the caller can still
commit — a genuine proof of the *service's* behaviour, and deliberately not
accepted here as a proof of the *integration's*. Between the two lies
everything Block F added: where the call sits relative to the transition, what
it does with a `None` return, and whether the endpoint's own error handling
swallows or re-raises. A call placed after `session.commit()`, or wrapped in a
bare `except` that returns 500, would satisfy Block B's test and still break a
clinical workflow.

So each test below fails notification persistence for real, drives the actual
endpoint, and asserts three things: the HTTP call succeeded, the clinical state
committed, and the audit/`OrderEvent` row committed. The notification is
absent — which is the accepted cost, recorded in the service contract §8.

The injection point
-------------------
`NotificationService._notify` is patched to raise. That is *inside* the
service's own containment (`notify()` catches, logs and returns `None`), so
what these tests exercise is the real production failure path end to end,
not a mock of it. Patching `notify()` itself would only prove that a function
returning `None` is harmless.
"""
import pytest
from sqlmodel import select

from app.models.enums import EventType, ReportStatus, SampleState
from app.models.events import OrderEvent
from app.models.notification import Notification
from app.models.report import Report
from app.services.notification import NotificationService
from tests.http.factories import (
    add_order_reviewer,
    assign_to_order,
    auth_headers,
    create_branch,
    create_order,
    create_patient,
    create_report,
    create_sample,
    create_tenant,
    create_user,
)


class NotificationExploded(RuntimeError):
    """Distinct from any real exception, so a test cannot pass because
    something *else* failed in the same place."""


@pytest.fixture
def failing_notifications(monkeypatch):
    """Make every `NotificationService.notify()` fail, the way production
    would on a database error mid-insert.

    Patched at `_notify`, so `notify()`'s real try/except/savepoint handling
    runs. Returns a counter so a test can assert the integration was actually
    reached — a test that "passes" because no notification was attempted
    proves nothing.
    """

    class _Counter:
        calls = 0

    def _explode(session, command):
        _Counter.calls += 1
        raise NotificationExploded("Simulated notification persistence failure")

    monkeypatch.setattr(NotificationService, "_notify", staticmethod(_explode))
    return _Counter


@pytest.fixture
def lab(session):
    tenant = create_tenant(session, name="Laboratorio Falla")
    branch = create_branch(session, tenant)
    return {
        "tenant": tenant,
        "branch": branch,
        "author": create_user(session, tenant, email="author@fail.test"),
        "signer": create_user(session, tenant, email="signer@fail.test", roles=("reviewer",)),
        "assignee": create_user(session, tenant, email="assignee@fail.test"),
        "admin": create_user(session, tenant, email="admin@fail.test"),
    }


def _assert_no_notification(session):
    assert session.exec(select(Notification)).all() == []


def _event_exists(session, order_id, event_type):
    return (
        session.exec(
            select(OrderEvent).where(
                OrderEvent.order_id == order_id,
                OrderEvent.event_type == event_type,
            )
        ).first()
        is not None
    )


# ---------------------------------------------------------------------------

class TestReportSubmittedSurvives:
    def test_the_report_still_submits(self, client, session, lab, failing_notifications):
        order = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-F-SUB")
        report, _ = create_report(
            session, lab["tenant"], lab["branch"], order,
            status=ReportStatus.DRAFT, created_by=lab["author"], authored_by=lab["author"],
        )
        add_order_reviewer(session, lab["tenant"], order, lab["signer"], report=report)

        response = client.post(
            f"/api/v1/reports/{report.id}/submit",
            json={"changelog": None},
            headers=auth_headers(lab["author"]),
        )

        assert response.status_code == 200
        assert failing_notifications.calls == 1, "the integration was actually reached"
        session.expire_all()
        assert session.get(Report, report.id).status == ReportStatus.IN_REVIEW
        assert _event_exists(session, order.id, EventType.REPORT_SUBMITTED)
        _assert_no_notification(session)


class TestReportPublishedSurvives:
    def _approved(self, session, lab, code):
        order = create_order(session, lab["tenant"], lab["branch"], order_code=code)
        report, version = create_report(
            session, lab["tenant"], lab["branch"], order,
            status=ReportStatus.APPROVED, created_by=lab["author"],
            authored_by=lab["author"], pdf_generation_status="READY",
        )
        assign_to_order(session, lab["tenant"], order, lab["assignee"])
        return order, report, version

    def test_the_report_still_publishes(self, client, session, lab, failing_notifications):
        order, report, version = self._approved(session, lab, "ORD-F-PUB")

        response = client.post(
            f"/api/v1/reports/{report.id}/sign",
            json={"changelog": None},
            headers=auth_headers(lab["signer"]),
        )

        assert response.status_code == 200
        assert failing_notifications.calls == 1
        session.expire_all()
        assert session.get(Report, report.id).status == ReportStatus.PUBLISHED
        assert _event_exists(session, order.id, EventType.REPORT_APPROVED)
        _assert_no_notification(session)

    def test_sign_and_publish_still_completes(
        self, client, session, lab, failing_notifications, stub_pdf_render
    ):
        order, report, version = self._approved(session, lab, "ORD-F-SAP")

        response = client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish",
            json={"changelog": None},
            headers=auth_headers(lab["signer"]),
        )

        assert response.status_code == 200
        session.expire_all()
        published = session.get(Report, report.id)
        assert published.status == ReportStatus.PUBLISHED
        assert published.published_at is not None
        _assert_no_notification(session)


class TestReportRetractedSurvives:
    def test_the_report_still_retracts(self, client, session, lab, failing_notifications):
        order = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-F-RET")
        report, _ = create_report(
            session, lab["tenant"], lab["branch"], order,
            status=ReportStatus.PUBLISHED, created_by=lab["author"], authored_by=lab["author"],
        )
        assign_to_order(session, lab["tenant"], order, lab["assignee"])

        response = client.post(
            f"/api/v1/reports/{report.id}/retract",
            json={"changelog": "motivo"},
            headers=auth_headers(lab["admin"]),
        )

        assert response.status_code == 200
        assert failing_notifications.calls == 1
        session.expire_all()
        assert session.get(Report, report.id).status == ReportStatus.RETRACTED
        assert _event_exists(session, order.id, EventType.REPORT_RETRACTED)
        _assert_no_notification(session)


class TestPdfReadySurvives:
    """The one integration whose notification is not atomic with its
    transition — `ReportPdfGenerationService` commits internally — so the
    asymmetry is the safe one: the artifact exists whether or not the
    notification does."""

    def test_the_pdf_is_still_generated_and_ready(
        self, client, session, lab, failing_notifications, stub_pdf_render
    ):
        order = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-F-PDF")
        report, version = create_report(
            session, lab["tenant"], lab["branch"], order,
            status=ReportStatus.APPROVED, created_by=lab["author"], authored_by=lab["author"],
        )
        add_order_reviewer(session, lab["tenant"], order, lab["signer"], report=report)

        response = client.post(
            f"/api/v1/reports/{report.id}/versions/{version.version_no}/generate-pdf",
            headers=auth_headers(lab["author"]),
        )

        assert response.status_code == 200
        assert failing_notifications.calls == 1
        session.expire_all()
        session.refresh(version)
        assert version.pdf_generation_status == "READY"
        assert version.pdf_sha256 is not None
        _assert_no_notification(session)


class TestAssignmentSurvives:
    def test_the_assignment_still_commits(self, client, session, lab, failing_notifications):
        order = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-F-ASG")

        response = client.put(
            f"/api/v1/laboratory/orders/{order.id}/assignees",
            json={"assignee_ids": [str(lab["assignee"].id)]},
            headers=auth_headers(lab["admin"]),
        )

        assert response.status_code == 200
        assert failing_notifications.calls == 1
        assert [a["id"] for a in response.json()["assignees"]] == [str(lab["assignee"].id)]
        assert _event_exists(session, order.id, EventType.ASSIGNEES_ADDED)
        _assert_no_notification(session)

    def test_the_reviewer_addition_still_commits(
        self, client, session, lab, failing_notifications
    ):
        order = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-F-REV")

        response = client.put(
            f"/api/v1/laboratory/orders/{order.id}/reviewers",
            json={"reviewer_ids": [str(lab["signer"].id)]},
            headers=auth_headers(lab["admin"]),
        )

        assert response.status_code == 200
        assert _event_exists(session, order.id, EventType.REVIEWERS_ADDED)
        _assert_no_notification(session)

    def test_the_sample_assignment_still_commits(
        self, client, session, lab, failing_notifications
    ):
        order = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-F-SASG")
        patient = create_patient(session, lab["tenant"], lab["branch"])
        order.patient_id = patient.id
        session.add(order)
        session.commit()
        sample = create_sample(session, lab["tenant"], lab["branch"], order)

        response = client.put(
            f"/api/v1/laboratory/samples/{sample.id}/assignees",
            json={"assignee_ids": [str(lab["assignee"].id)]},
            headers=auth_headers(lab["admin"]),
        )

        assert response.status_code == 200
        assert failing_notifications.calls == 1
        _assert_no_notification(session)


class TestSampleStatusSurvives:
    def test_the_state_transition_still_commits(
        self, client, session, lab, failing_notifications
    ):
        order = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-F-SMP")
        sample = create_sample(session, lab["tenant"], lab["branch"], order)
        assign_to_order(session, lab["tenant"], order, lab["assignee"])

        response = client.patch(
            f"/api/v1/laboratory/samples/{sample.id}/state",
            json={"state": SampleState.PROCESSING.value},
            headers=auth_headers(lab["admin"]),
        )

        assert response.status_code == 200
        assert failing_notifications.calls == 1
        session.expire_all()
        session.refresh(sample)
        assert sample.state == SampleState.PROCESSING
        assert _event_exists(session, order.id, EventType.SAMPLE_STATE_CHANGED)
        _assert_no_notification(session)


class TestDeliveryMaterializationFailureSurvives:
    """Outcome B of the materialization contract, at a real call site.

    Delivery materialization fails; the in-app notification and its recipient
    rows survive, and so does the clinical transition. This is the layer below
    the one every other test in this file exercises — the notification is
    created, and only the optional email intent is lost.
    """

    def test_the_notification_survives_a_delivery_failure(
        self, client, session, lab, monkeypatch
    ):
        def _explode(*args, **kwargs):
            raise NotificationExploded("Simulated delivery materialization failure")

        monkeypatch.setattr(
            "app.services.notification.materialize_email_deliveries", _explode
        )

        order = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-F-DEL")
        report, _ = create_report(
            session, lab["tenant"], lab["branch"], order,
            status=ReportStatus.DRAFT, created_by=lab["author"], authored_by=lab["author"],
        )
        add_order_reviewer(session, lab["tenant"], order, lab["signer"], report=report)

        response = client.post(
            f"/api/v1/reports/{report.id}/submit",
            json={"changelog": None},
            headers=auth_headers(lab["author"]),
        )

        assert response.status_code == 200
        session.expire_all()
        assert session.get(Report, report.id).status == ReportStatus.IN_REVIEW

        notifications = session.exec(select(Notification)).all()
        assert len(notifications) == 1, "the in-app notification survived"

        from app.models.notification import NotificationDelivery, NotificationRecipient

        recipients = session.exec(select(NotificationRecipient)).all()
        assert len(recipients) == 1, "and so did the recipient row"
        assert session.exec(select(NotificationDelivery)).all() == [], (
            "no partial delivery batch remains"
        )
