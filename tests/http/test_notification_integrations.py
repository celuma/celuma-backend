"""Domain integration tests (Céluma 1.3, Phase 3, Block F, Story F14).

The first block in which a notification comes from a real clinical
transition, so these drive the actual endpoints — `POST /reports/{id}/submit`,
`PUT /orders/{id}/assignees`, `PATCH /samples/{id}/state` — and assert on what
landed in the database, rather than calling `NotificationService` directly the
way Blocks B–E had to.

Structure mirrors Story F14's four groups: trigger correctness, recipient
correctness, preference/delivery, and privacy.
"""
import uuid

import pytest
from sqlmodel import select

from app.models.enums import ReportStatus, SampleState
from app.models.notification import (
    Notification,
    NotificationDelivery,
    NotificationRecipient,
    NotificationType,
)
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
    deliveries_for,
    notifications_for,
    set_email_preference,
)


# ---------------------------------------------------------------------------
# Sentinels — Story F14's privacy assertions
# ---------------------------------------------------------------------------
#
# Values planted in the clinical fields a notification must never carry. Every
# test that performs a transition plants them, and `assert_no_sentinels_leaked`
# sweeps the whole notification graph for them at the end. A sentinel is used
# rather than a realistic value so a leak is unambiguous: "PATIENT_SECRET_..."
# cannot appear in a notification by coincidence.

PATIENT_SECRET_SENTINEL = "PATIENT_SECRET_SENTINEL"
DIAGNOSIS_SECRET_SENTINEL = "DIAGNOSIS_SECRET_SENTINEL"
RETRACTION_SECRET_SENTINEL = "RETRACTION_SECRET_SENTINEL"
COMMENT_SECRET_SENTINEL = "COMMENT_SECRET_SENTINEL"

ALL_SENTINELS = (
    PATIENT_SECRET_SENTINEL,
    DIAGNOSIS_SECRET_SENTINEL,
    RETRACTION_SECRET_SENTINEL,
    COMMENT_SECRET_SENTINEL,
)


def assert_no_sentinels_leaked(session):
    """No sentinel appears anywhere in the notification graph.

    Sweeps every notification's title, body and metadata blob (which holds
    `template_key` and `template_params`) plus every delivery's error code —
    the four places stored text could surface. A single failure names which
    sentinel and which field, so the assertion is diagnostic rather than just
    red.
    """
    for notification in session.exec(select(Notification)).all():
        haystack = " ".join(
            [
                notification.title or "",
                notification.body or "",
                repr(notification.notification_metadata or {}),
            ]
        )
        for sentinel in ALL_SENTINELS:
            assert sentinel not in haystack, (
                f"{sentinel} leaked into notification {notification.id} "
                f"({notification.type})"
            )
    for delivery in session.exec(select(NotificationDelivery)).all():
        for sentinel in ALL_SENTINELS:
            assert sentinel not in (delivery.error_code or "")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def lab(session):
    """A tenant with the cast every event below needs.

    `author` writes reports, `reviewer_signer` reviews and can sign,
    `reviewer_plain` reviews but cannot, `assignee` works the order,
    `outsider` is in the tenant but touches nothing, and `inactive` is
    deactivated. Roles are the real seeded system roles assigned through
    `assign_role_by_code`, exactly as registration does — so a permission
    assertion here is an assertion about the production RBAC catalog.
    """
    tenant = create_tenant(session, name="Laboratorio Sentinel")
    branch = create_branch(session, tenant)
    users = {
        "author": create_user(session, tenant, email="author@lab.test"),
        "reviewer_signer": create_user(
            session, tenant, email="signer@lab.test", roles=("reviewer",)
        ),
        "reviewer_plain": create_user(
            session, tenant, email="plain@lab.test", roles=("assistant",)
        ),
        "assignee": create_user(session, tenant, email="assignee@lab.test"),
        "outsider": create_user(session, tenant, email="outsider@lab.test"),
        "inactive": create_user(session, tenant, email="inactive@lab.test"),
        "admin": create_user(session, tenant, email="admin@lab.test"),
    }
    users["inactive"].is_active = False
    session.add(users["inactive"])
    session.commit()
    return {"tenant": tenant, "branch": branch, **users}


@pytest.fixture
def other_lab(session):
    """A second tenant, for the isolation assertions."""
    tenant = create_tenant(session, name="Otro Laboratorio")
    branch = create_branch(session, tenant)
    user = create_user(session, tenant, email="stranger@other.test")
    return {"tenant": tenant, "branch": branch, "user": user}


# ---------------------------------------------------------------------------
# REPORT_SUBMITTED
# ---------------------------------------------------------------------------

class TestReportSubmitted:
    def _setup(self, session, lab, *, reviewers):
        order = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-SUB-1")
        report, version = create_report(
            session,
            lab["tenant"],
            lab["branch"],
            order,
            status=ReportStatus.DRAFT,
            created_by=lab["author"],
            authored_by=lab["author"],
        )
        report.title = PATIENT_SECRET_SENTINEL
        session.add(report)
        for reviewer in reviewers:
            add_order_reviewer(session, lab["tenant"], order, reviewer, report=report)
        session.commit()
        return order, report

    def test_submitting_notifies_the_order_reviewers(self, client, session, lab):
        order, report = self._setup(
            session, lab, reviewers=[lab["reviewer_signer"], lab["reviewer_plain"]]
        )

        response = client.post(
            f"/api/v1/reports/{report.id}/submit",
            json={"changelog": COMMENT_SECRET_SENTINEL},
            headers=auth_headers(lab["author"]),
        )
        assert response.status_code == 200

        for reviewer in (lab["reviewer_signer"], lab["reviewer_plain"]):
            rows = notifications_for(
                session, reviewer, notification_type=NotificationType.REPORT_SUBMITTED
            )
            assert len(rows) == 1
            notification, _ = rows[0]
            assert notification.resource_type == "report"
            assert notification.resource_id == report.id
            assert notification.notification_metadata["template_key"] == "report_submitted_v1"
            assert notification.created_by == lab["author"].id
            assert notification.locale == "es-MX"
            assert "ORD-SUB-1" in notification.title

        assert_no_sentinels_leaked(session)

    def test_the_submitting_author_is_not_notified_even_when_also_a_reviewer(
        self, client, session, lab
    ):
        """Matrix rule: the actor is excluded, and a person should not be asked
        to review their own submission."""
        order, report = self._setup(
            session, lab, reviewers=[lab["author"], lab["reviewer_signer"]]
        )

        client.post(
            f"/api/v1/reports/{report.id}/submit",
            json={"changelog": None},
            headers=auth_headers(lab["author"]),
        )

        assert notifications_for(session, lab["author"]) == []
        assert len(notifications_for(session, lab["reviewer_signer"])) == 1

    def test_an_unrelated_user_and_an_inactive_reviewer_are_excluded(
        self, client, session, lab
    ):
        order, report = self._setup(
            session, lab, reviewers=[lab["reviewer_signer"], lab["inactive"]]
        )

        client.post(
            f"/api/v1/reports/{report.id}/submit",
            json={"changelog": None},
            headers=auth_headers(lab["author"]),
        )

        assert notifications_for(session, lab["outsider"]) == []
        assert notifications_for(session, lab["inactive"]) == []
        assert notifications_for(session, lab["admin"]) == [], (
            "no blanket admin fan-out — matrix cross-cutting rule 2"
        )
        assert len(notifications_for(session, lab["reviewer_signer"])) == 1

    def test_a_second_review_cycle_on_the_same_version_notifies_again(
        self, client, session, lab
    ):
        """The case that makes `report_version.id` the wrong marker.

        `request-changes` sends the report back to DRAFT without creating a new
        `ReportVersion`, so a second submission is a second legitimate review
        request on the same version row. Keying the marker on the version would
        silently drop it; keying on the `OrderEvent` does not.
        """
        order, report = self._setup(session, lab, reviewers=[lab["reviewer_signer"]])

        client.post(
            f"/api/v1/reports/{report.id}/submit",
            json={"changelog": None},
            headers=auth_headers(lab["author"]),
        )
        # Back to DRAFT, same version, then submit again.
        session.refresh(report)
        report.status = ReportStatus.DRAFT
        session.add(report)
        session.commit()
        client.post(
            f"/api/v1/reports/{report.id}/submit",
            json={"changelog": None},
            headers=auth_headers(lab["author"]),
        )

        rows = notifications_for(
            session, lab["reviewer_signer"], notification_type=NotificationType.REPORT_SUBMITTED
        )
        assert len(rows) == 2
        assert rows[0][0].id != rows[1][0].id

    def test_a_repeated_request_against_a_non_draft_report_creates_nothing(
        self, client, session, lab
    ):
        """The no-op case: the domain guard rejects the second POST, so no
        second occurrence exists to deduplicate."""
        order, report = self._setup(session, lab, reviewers=[lab["reviewer_signer"]])

        first = client.post(
            f"/api/v1/reports/{report.id}/submit",
            json={"changelog": None},
            headers=auth_headers(lab["author"]),
        )
        second = client.post(
            f"/api/v1/reports/{report.id}/submit",
            json={"changelog": None},
            headers=auth_headers(lab["author"]),
        )

        assert first.status_code == 200
        assert second.status_code == 400
        assert len(notifications_for(session, lab["reviewer_signer"])) == 1

    def test_no_reviewers_does_not_block_the_domain_operation(
        self, client, session, lab
    ):
        """`submit_report` requires ≥1 reviewer, so this asserts the guard
        rather than an empty-recipient notification — the domain refuses
        first, and no notification is reached."""
        order, report = self._setup(session, lab, reviewers=[])

        response = client.post(
            f"/api/v1/reports/{report.id}/submit",
            json={"changelog": None},
            headers=auth_headers(lab["author"]),
        )
        assert response.status_code == 400

    def test_the_notification_carries_no_metadata_beyond_template_provenance(
        self, client, session, lab
    ):
        """Story F10: `extra_metadata` is not used by any integration.

        The persisted blob holds exactly `template_key` and `template_params`
        — the provenance `NotificationService` writes itself — and nothing a
        call site added.
        """
        order, report = self._setup(session, lab, reviewers=[lab["reviewer_signer"]])
        client.post(
            f"/api/v1/reports/{report.id}/submit",
            json={"changelog": None},
            headers=auth_headers(lab["author"]),
        )

        notification, _ = notifications_for(session, lab["reviewer_signer"])[0]
        assert set(notification.notification_metadata) == {"template_key", "template_params"}
        assert set(notification.notification_metadata["template_params"]) == {
            "order_number",
            "actor_name",
        }


# ---------------------------------------------------------------------------
# REPORT_PUBLISHED / REPORT_RETRACTED
# ---------------------------------------------------------------------------

class TestReportPublishedAndRetracted:
    def _approved_report(self, session, lab, *, order_code="ORD-PUB-1"):
        order = create_order(session, lab["tenant"], lab["branch"], order_code=order_code)
        report, version = create_report(
            session,
            lab["tenant"],
            lab["branch"],
            order,
            status=ReportStatus.APPROVED,
            created_by=lab["author"],
            authored_by=lab["author"],
            pdf_generation_status="READY",
        )
        assign_to_order(session, lab["tenant"], order, lab["assignee"])
        return order, report, version

    def test_publishing_notifies_assignees_and_the_author_but_not_the_signer(
        self, client, session, lab
    ):
        order, report, version = self._approved_report(session, lab)

        response = client.post(
            f"/api/v1/reports/{report.id}/sign",
            json={"changelog": COMMENT_SECRET_SENTINEL},
            headers=auth_headers(lab["reviewer_signer"]),
        )
        assert response.status_code == 200

        for recipient in (lab["assignee"], lab["author"]):
            rows = notifications_for(
                session, recipient, notification_type=NotificationType.REPORT_PUBLISHED
            )
            assert len(rows) == 1, f"{recipient.email} should have been notified"
            assert rows[0][0].resource_id == report.id

        assert notifications_for(
            session, lab["reviewer_signer"], notification_type=NotificationType.REPORT_PUBLISHED
        ) == []
        assert_no_sentinels_leaked(session)

    def test_retracting_notifies_the_same_set_and_never_carries_the_reason(
        self, client, session, lab
    ):
        order, report, version = self._approved_report(session, lab, order_code="ORD-RET-1")
        client.post(
            f"/api/v1/reports/{report.id}/sign",
            json={"changelog": None},
            headers=auth_headers(lab["reviewer_signer"]),
        )

        response = client.post(
            f"/api/v1/reports/{report.id}/retract",
            json={"changelog": RETRACTION_SECRET_SENTINEL},
            headers=auth_headers(lab["admin"]),
        )
        assert response.status_code == 200

        for recipient in (lab["assignee"], lab["author"]):
            rows = notifications_for(
                session, recipient, notification_type=NotificationType.REPORT_RETRACTED
            )
            assert len(rows) == 1
            notification, _ = rows[0]
            assert RETRACTION_SECRET_SENTINEL not in (notification.title or "")
            assert RETRACTION_SECRET_SENTINEL not in (notification.body or "")
            assert RETRACTION_SECRET_SENTINEL not in repr(
                notification.notification_metadata
            )

        assert_no_sentinels_leaked(session)

    def test_the_retraction_reason_still_reaches_the_timeline(
        self, client, session, lab
    ):
        """The reason is withheld from the notification, not from the system.

        It stays on the `OrderEvent`, where normal RBAC governs who reads it —
        which is what makes withholding it from the notification a privacy
        decision rather than data loss.
        """
        from app.models.enums import EventType
        from app.models.events import OrderEvent

        order, report, version = self._approved_report(session, lab, order_code="ORD-RET-2")
        client.post(
            f"/api/v1/reports/{report.id}/sign",
            json={"changelog": None},
            headers=auth_headers(lab["reviewer_signer"]),
        )
        client.post(
            f"/api/v1/reports/{report.id}/retract",
            json={"changelog": RETRACTION_SECRET_SENTINEL},
            headers=auth_headers(lab["admin"]),
        )

        event = session.exec(
            select(OrderEvent).where(
                OrderEvent.order_id == order.id,
                OrderEvent.event_type == EventType.REPORT_RETRACTED,
            )
        ).first()
        assert event.event_metadata["reason"] == RETRACTION_SECRET_SENTINEL


# ---------------------------------------------------------------------------
# REPORT_PDF_READY
# ---------------------------------------------------------------------------

class TestReportPdfReady:
    def _approved_report(self, session, lab):
        order = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-PDF-1")
        report, version = create_report(
            session,
            lab["tenant"],
            lab["branch"],
            order,
            status=ReportStatus.APPROVED,
            created_by=lab["author"],
            authored_by=lab["author"],
        )
        add_order_reviewer(session, lab["tenant"], order, lab["reviewer_signer"], report=report)
        add_order_reviewer(session, lab["tenant"], order, lab["reviewer_plain"], report=report)
        return order, report, version

    def test_generation_notifies_only_reviewers_who_can_sign(
        self, client, session, lab, stub_pdf_render
    ):
        order, report, version = self._approved_report(session, lab)

        response = client.post(
            f"/api/v1/reports/{report.id}/versions/{version.version_no}/generate-pdf",
            headers=auth_headers(lab["author"]),
        )
        assert response.status_code == 200

        assert len(
            notifications_for(
                session, lab["reviewer_signer"], notification_type=NotificationType.REPORT_PDF_READY
            )
        ) == 1
        assert notifications_for(
            session, lab["reviewer_plain"], notification_type=NotificationType.REPORT_PDF_READY
        ) == [], "a reviewer without reports:sign cannot act on a ready PDF"

    def test_an_idempotent_regeneration_request_notifies_nobody_again(
        self, client, session, lab, stub_pdf_render
    ):
        """Story F4: "Do not notify users for idempotent requests against a PDF
        already READY." The endpoint checks the pre-call status, so the second
        POST does not even reach the integration."""
        order, report, version = self._approved_report(session, lab)
        url = f"/api/v1/reports/{report.id}/versions/{version.version_no}/generate-pdf"

        client.post(url, headers=auth_headers(lab["author"]))
        client.post(url, headers=auth_headers(lab["author"]))

        assert len(
            notifications_for(
                session, lab["reviewer_signer"], notification_type=NotificationType.REPORT_PDF_READY
            )
        ) == 1
        assert stub_pdf_render.call_count == 1, "the second call short-circuited"

    def test_sign_and_publish_does_not_emit_a_pdf_ready_notification(
        self, client, session, lab, stub_pdf_render
    ):
        """Its forced regeneration is an internal step of publishing, not an
        invitation to sign something already being signed."""
        order, report, version = self._approved_report(session, lab)

        response = client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish",
            json={"changelog": None},
            headers=auth_headers(lab["reviewer_signer"]),
        )
        assert response.status_code == 200

        pdf_ready = session.exec(
            select(Notification).where(
                Notification.type == NotificationType.REPORT_PDF_READY.value
            )
        ).all()
        assert pdf_ready == []
        published = session.exec(
            select(Notification).where(
                Notification.type == NotificationType.REPORT_PUBLISHED.value
            )
        ).all()
        assert len(published) == 1


# ---------------------------------------------------------------------------
# ASSIGNMENT_ADDED
# ---------------------------------------------------------------------------

class TestAssignmentAdded:
    def test_only_the_newly_added_user_is_notified(self, client, session, lab):
        order = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-ASG-1")
        assign_to_order(session, lab["tenant"], order, lab["assignee"])

        response = client.put(
            f"/api/v1/laboratory/orders/{order.id}/assignees",
            json={"assignee_ids": [str(lab["assignee"].id), str(lab["outsider"].id)]},
            headers=auth_headers(lab["admin"]),
        )
        assert response.status_code == 200

        assert len(
            notifications_for(
                session, lab["outsider"], notification_type=NotificationType.ASSIGNMENT_ADDED
            )
        ) == 1
        assert notifications_for(
            session, lab["assignee"], notification_type=NotificationType.ASSIGNMENT_ADDED
        ) == [], "an already-assigned user is not re-notified"

    def test_a_repeated_identical_request_creates_no_duplicate(self, client, session, lab):
        order = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-ASG-2")
        payload = {"assignee_ids": [str(lab["assignee"].id)]}
        url = f"/api/v1/laboratory/orders/{order.id}/assignees"

        client.put(url, json=payload, headers=auth_headers(lab["admin"]))
        client.put(url, json=payload, headers=auth_headers(lab["admin"]))

        assert len(
            notifications_for(
                session, lab["assignee"], notification_type=NotificationType.ASSIGNMENT_ADDED
            )
        ) == 1

    def test_a_removal_only_request_notifies_nobody(self, client, session, lab):
        order = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-ASG-3")
        url = f"/api/v1/laboratory/orders/{order.id}/assignees"
        client.put(
            url,
            json={"assignee_ids": [str(lab["assignee"].id)]},
            headers=auth_headers(lab["admin"]),
        )
        before = len(session.exec(select(Notification)).all())

        client.put(url, json={"assignee_ids": []}, headers=auth_headers(lab["admin"]))

        assert len(session.exec(select(Notification)).all()) == before

    def test_adding_two_users_creates_two_independently_keyed_notifications(
        self, client, session, lab
    ):
        """One `OrderEvent` covers the whole `added` set, so the marker has to
        carry the recipient too — otherwise the first user's notification would
        suppress the second's."""
        order = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-ASG-4")

        client.put(
            f"/api/v1/laboratory/orders/{order.id}/assignees",
            json={"assignee_ids": [str(lab["assignee"].id), str(lab["outsider"].id)]},
            headers=auth_headers(lab["admin"]),
        )

        for recipient in (lab["assignee"], lab["outsider"]):
            assert len(
                notifications_for(
                    session, recipient, notification_type=NotificationType.ASSIGNMENT_ADDED
                )
            ) == 1
        keys = {
            n.idempotency_key
            for n in session.exec(
                select(Notification).where(
                    Notification.type == NotificationType.ASSIGNMENT_ADDED.value
                )
            ).all()
        }
        assert len(keys) == 2

    def test_an_actor_assigning_themself_is_not_notified(self, client, session, lab):
        order = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-ASG-5")

        client.put(
            f"/api/v1/laboratory/orders/{order.id}/assignees",
            json={"assignee_ids": [str(lab["admin"].id)]},
            headers=auth_headers(lab["admin"]),
        )

        assert notifications_for(
            session, lab["admin"], notification_type=NotificationType.ASSIGNMENT_ADDED
        ) == []

    def test_an_order_assignment_uses_the_order_template(self, client, session, lab):
        """Pre-release remediation: order-context assignment keeps using the
        original `assignment_added_v1` key — its meaning did not change."""
        order = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-ASG-6B")

        response = client.put(
            f"/api/v1/laboratory/orders/{order.id}/assignees",
            json={"assignee_ids": [str(lab["assignee"].id)]},
            headers=auth_headers(lab["admin"]),
        )
        assert response.status_code == 200

        notification, _ = notifications_for(
            session, lab["assignee"], notification_type=NotificationType.ASSIGNMENT_ADDED
        )[0]
        assert notification.resource_type == "order"
        assert notification.resource_id == order.id
        assert notification.notification_metadata["template_key"] == "assignment_added_v1"
        assert "ORD-ASG-6B" in notification.title
        assert "asignó a esta orden" in (notification.body or "")

    def test_a_sample_assignment_points_at_the_sample(self, client, session, lab):
        order = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-ASG-6")
        # `SampleDetailResponse` requires a patient; this endpoint's response
        # model, not the notification, is what needs it.
        patient = create_patient(session, lab["tenant"], lab["branch"])
        order.patient_id = patient.id
        session.add(order)
        session.commit()
        sample = create_sample(session, lab["tenant"], lab["branch"], order, sample_code="S-ASG-6")

        response = client.put(
            f"/api/v1/laboratory/samples/{sample.id}/assignees",
            json={"assignee_ids": [str(lab["assignee"].id)]},
            headers=auth_headers(lab["admin"]),
        )
        assert response.status_code == 200

        notification, _ = notifications_for(
            session, lab["assignee"], notification_type=NotificationType.ASSIGNMENT_ADDED
        )[0]
        assert notification.resource_type == "sample"
        assert notification.resource_id == sample.id
        assert "ORD-ASG-6" in notification.title

        # Pre-release remediation: sample assignment must not be indistinguishable
        # from order assignment — it names the sample and uses its own key.
        assert notification.notification_metadata["template_key"] == "assignment_added_sample_v1"
        assert "de muestra" in notification.title
        assert "S-ASG-6" in (notification.body or "")
        assert notification.resource_type == "sample", (
            "the deep link must open the sample, not the order"
        )

    def test_adding_a_reviewer_notifies_them(self, client, session, lab):
        order = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-ASG-7")

        response = client.put(
            f"/api/v1/laboratory/orders/{order.id}/reviewers",
            json={"reviewer_ids": [str(lab["reviewer_signer"].id)]},
            headers=auth_headers(lab["admin"]),
        )
        assert response.status_code == 200

        notification, _ = notifications_for(
            session, lab["reviewer_signer"], notification_type=NotificationType.ASSIGNMENT_ADDED
        )[0]
        assert notification.resource_type == "order"
        assert notification.resource_id == order.id

        # Pre-release remediation: reviewer addition must read as "you were
        # asked to review", not the generic order-assignee copy.
        assert notification.notification_metadata["template_key"] == "assignment_added_review_v1"
        assert "revisión" in notification.title.lower()
        assert "revisión" in (notification.body or "").lower()


# ---------------------------------------------------------------------------
# SAMPLE_STATUS_CHANGED
# ---------------------------------------------------------------------------

class TestSampleStatusChanged:
    def test_a_state_change_notifies_order_assignees_in_app_only(
        self, client, session, lab
    ):
        order = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-SMP-1")
        sample = create_sample(session, lab["tenant"], lab["branch"], order, sample_code="S-77")
        assign_to_order(session, lab["tenant"], order, lab["assignee"])

        response = client.patch(
            f"/api/v1/laboratory/samples/{sample.id}/state",
            json={"state": SampleState.PROCESSING.value},
            headers=auth_headers(lab["admin"]),
        )
        assert response.status_code == 200

        rows = notifications_for(
            session, lab["assignee"], notification_type=NotificationType.SAMPLE_STATUS_CHANGED
        )
        assert len(rows) == 1
        notification, _ = rows[0]
        assert notification.resource_type == "sample"
        assert notification.resource_id == sample.id
        assert "S-77" in (notification.body or "")
        assert deliveries_for(session, notification.id) == [], (
            "SAMPLE_STATUS_CHANGED is email_supported = False — no delivery row, ever"
        )

        # Pre-release remediation: the raw English enum value must never
        # reach the rendered Spanish body — it must carry the translated
        # label instead, via the new template version.
        assert notification.notification_metadata["template_key"] == "sample_status_changed_v2"
        assert "PROCESSING" not in (notification.body or "")
        assert "En proceso" in (notification.body or "")

    @pytest.mark.parametrize(
        "state,label",
        [
            (SampleState.RECEIVED, "Recibida"),
            (SampleState.PROCESSING, "En proceso"),
            (SampleState.READY, "Lista"),
            (SampleState.DAMAGED, "Insuficiente"),
            (SampleState.CANCELLED, "Cancelada"),
        ],
    )
    def test_every_sample_state_renders_its_es_mx_label_not_the_raw_enum(
        self, client, session, lab, state, label
    ):
        # Start from a state guaranteed different from the target — PATCHing
        # to the sample's current state is a no-op that creates no
        # notification (tested above), which would make the RECEIVED case
        # vacuous since RECEIVED is the factory default.
        initial_state = SampleState.PROCESSING if state == SampleState.RECEIVED else SampleState.RECEIVED
        order = create_order(session, lab["tenant"], lab["branch"], order_code=f"ORD-LBL-{state.value}")
        sample = create_sample(
            session, lab["tenant"], lab["branch"], order, sample_code="S-LBL", state=initial_state
        )
        assign_to_order(session, lab["tenant"], order, lab["assignee"])

        response = client.patch(
            f"/api/v1/laboratory/samples/{sample.id}/state",
            json={"state": state.value},
            headers=auth_headers(lab["admin"]),
        )
        assert response.status_code == 200

        notification, _ = notifications_for(
            session, lab["assignee"], notification_type=NotificationType.SAMPLE_STATUS_CHANGED
        )[0]
        assert label in (notification.body or "")
        assert state.value not in (notification.body or "")

    def test_a_no_op_state_request_creates_no_notification(self, client, session, lab):
        """The endpoint writes a timeline row regardless; the integration
        declines. Without this, a client re-sending the same state would
        produce a fresh marker the idempotency key could not catch."""
        order = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-SMP-2")
        sample = create_sample(
            session, lab["tenant"], lab["branch"], order, state=SampleState.PROCESSING
        )
        assign_to_order(session, lab["tenant"], order, lab["assignee"])

        client.patch(
            f"/api/v1/laboratory/samples/{sample.id}/state",
            json={"state": SampleState.PROCESSING.value},
            headers=auth_headers(lab["admin"]),
        )

        assert notifications_for(
            session, lab["assignee"], notification_type=NotificationType.SAMPLE_STATUS_CHANGED
        ) == []

    def test_a_real_back_and_forth_sequence_notifies_each_time(
        self, client, session, lab
    ):
        """RECEIVED -> PROCESSING -> RECEIVED -> PROCESSING is three real
        transitions and three notifications. Each has its own `OrderEvent`, so
        each has its own marker."""
        order = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-SMP-3")
        sample = create_sample(session, lab["tenant"], lab["branch"], order)
        assign_to_order(session, lab["tenant"], order, lab["assignee"])
        url = f"/api/v1/laboratory/samples/{sample.id}/state"

        for state in ("PROCESSING", "RECEIVED", "PROCESSING"):
            client.patch(url, json={"state": state}, headers=auth_headers(lab["admin"]))

        assert len(
            notifications_for(
                session, lab["assignee"], notification_type=NotificationType.SAMPLE_STATUS_CHANGED
            )
        ) == 3

    def test_no_assignees_leaves_an_audit_row_and_does_not_fail_the_transition(
        self, client, session, lab
    ):
        """Matrix rule 6: the notification is still the record that the event
        happened, with zero recipient rows."""
        order = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-SMP-4")
        sample = create_sample(session, lab["tenant"], lab["branch"], order)

        response = client.patch(
            f"/api/v1/laboratory/samples/{sample.id}/state",
            json={"state": SampleState.READY.value},
            headers=auth_headers(lab["admin"]),
        )
        assert response.status_code == 200
        session.refresh(sample)
        assert sample.state == SampleState.READY

        notification = session.exec(
            select(Notification).where(
                Notification.type == NotificationType.SAMPLE_STATUS_CHANGED.value
            )
        ).first()
        assert notification is not None
        recipients = session.exec(
            select(NotificationRecipient).where(
                NotificationRecipient.notification_id == notification.id
            )
        ).all()
        assert recipients == []


# ---------------------------------------------------------------------------
# Preferences and delivery materialization (Story F11)
# ---------------------------------------------------------------------------

class TestPreferencesAndDelivery:
    def _submit(self, client, session, lab, order_code):
        order = create_order(session, lab["tenant"], lab["branch"], order_code=order_code)
        report, _ = create_report(
            session,
            lab["tenant"],
            lab["branch"],
            order,
            status=ReportStatus.DRAFT,
            created_by=lab["author"],
            authored_by=lab["author"],
        )
        add_order_reviewer(session, lab["tenant"], order, lab["reviewer_signer"], report=report)
        client.post(
            f"/api/v1/reports/{report.id}/submit",
            json={"changelog": None},
            headers=auth_headers(lab["author"]),
        )
        return notifications_for(
            session, lab["reviewer_signer"], notification_type=NotificationType.REPORT_SUBMITTED
        )[0][0]

    def test_the_default_preference_materializes_a_pending_delivery(
        self, client, session, lab
    ):
        notification = self._submit(client, session, lab, "ORD-PRF-1")

        deliveries = deliveries_for(session, notification.id)
        assert len(deliveries) == 1
        delivery = deliveries[0]
        assert delivery.status == "PENDING"
        assert delivery.recipient_user_id == lab["reviewer_signer"].id
        assert delivery.recipient_address == "signer@lab.test"
        assert delivery.attempts == 0

    def test_an_explicit_disable_prevents_the_delivery_but_not_the_inbox_row(
        self, client, session, lab
    ):
        set_email_preference(
            session,
            lab["tenant"],
            lab["reviewer_signer"],
            NotificationType.REPORT_SUBMITTED,
            enabled=False,
        )

        notification = self._submit(client, session, lab, "ORD-PRF-2")

        assert deliveries_for(session, notification.id) == []
        assert len(
            notifications_for(
                session, lab["reviewer_signer"], notification_type=NotificationType.REPORT_SUBMITTED
            )
        ) == 1

    def test_re_enabling_makes_a_later_occurrence_deliverable_again(
        self, client, session, lab
    ):
        """Preferences affect *future* notifications. An already-created
        notification is never revisited — the preference is read once, at
        materialization."""
        from app.models.notification import NotificationPreference

        set_email_preference(
            session,
            lab["tenant"],
            lab["reviewer_signer"],
            NotificationType.REPORT_SUBMITTED,
            enabled=False,
        )
        first = self._submit(client, session, lab, "ORD-PRF-3")
        assert deliveries_for(session, first.id) == []

        preference = session.exec(
            select(NotificationPreference).where(
                NotificationPreference.user_id == lab["reviewer_signer"].id
            )
        ).first()
        preference.email_enabled = True
        session.add(preference)
        session.commit()

        second = self._submit(client, session, lab, "ORD-PRF-4")
        assert len(deliveries_for(session, second.id)) == 1
        assert deliveries_for(session, first.id) == [], "the earlier one is not revisited"

    def test_a_recipient_without_a_usable_email_still_gets_the_inbox_row(
        self, client, session, lab
    ):
        lab["reviewer_signer"].email = "not-an-address"
        session.add(lab["reviewer_signer"])
        session.commit()

        notification = self._submit(client, session, lab, "ORD-PRF-5")

        assert deliveries_for(session, notification.id) == []
        assert len(
            notifications_for(
                session, lab["reviewer_signer"], notification_type=NotificationType.REPORT_SUBMITTED
            )
        ) == 1


# ---------------------------------------------------------------------------
# Tenant isolation (Story F14)
# ---------------------------------------------------------------------------

class TestTenantIsolation:
    def test_a_transition_in_one_tenant_is_invisible_in_another(
        self, client, session, lab, other_lab
    ):
        order = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-ISO-1")
        report, _ = create_report(
            session,
            lab["tenant"],
            lab["branch"],
            order,
            status=ReportStatus.DRAFT,
            created_by=lab["author"],
            authored_by=lab["author"],
        )
        add_order_reviewer(session, lab["tenant"], order, lab["reviewer_signer"], report=report)

        client.post(
            f"/api/v1/reports/{report.id}/submit",
            json={"changelog": None},
            headers=auth_headers(lab["author"]),
        )

        assert notifications_for(session, other_lab["user"]) == []
        for notification in session.exec(select(Notification)).all():
            assert notification.tenant_id == lab["tenant"].id
        for recipient in session.exec(select(NotificationRecipient)).all():
            assert recipient.tenant_id == lab["tenant"].id
        for delivery in session.exec(select(NotificationDelivery)).all():
            assert delivery.tenant_id == lab["tenant"].id

    def test_the_other_tenants_inbox_endpoint_returns_nothing(
        self, client, session, lab, other_lab
    ):
        order = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-ISO-2")
        sample = create_sample(session, lab["tenant"], lab["branch"], order)
        assign_to_order(session, lab["tenant"], order, lab["assignee"])
        client.patch(
            f"/api/v1/laboratory/samples/{sample.id}/state",
            json={"state": SampleState.READY.value},
            headers=auth_headers(lab["admin"]),
        )

        response = client.get(
            "/api/v1/notifications", headers=auth_headers(other_lab["user"])
        )
        assert response.status_code == 200
        assert response.json()["items"] == []
