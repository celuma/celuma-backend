"""Céluma 1.4 (#13): amendments of signed reports, end to end over HTTP on real
Postgres.

Contract: celuma-engineering `specs/reports/lifecycle.md` ("Amendments of
signed reports", "Lifecycle events") and ADR 0018. Owner decisions D1–D6
(2026-10-18): complete superseding re-emission (corrections and IHC
supplements alike), identification + required change note, the last signed
emission delivered until republication, password-and-reason cancellation,
in-app republication notice, V2 originals only, report and order follow the
amendment cycle.

Every transition is driven through the real routes (no direct state
fabrication except where a test needs an impossible-by-API state), and each
asserts the audit row AND the timeline row, exactly once, with correlation.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from app.models.audit import AuditLog
from app.models.enums import EventType, OrderStatus, ReportStatus, ReviewStatus, SampleState
from app.models.events import OrderEvent
from app.models.laboratory import Order
from app.models.notification import Notification, NotificationDelivery
from app.models.report import Report, ReportVersion
from app.models.report_amendment import ReportAmendment
from app.models.report_review import ReportReview
from app.models.storage import StorageObject
from app.services.password_confirmation import PASSWORD_CONFIRMATION_FAILED_ACTION

from .conftest import FakeS3Service, make_pdf_bytes
from .factories import (
    TEST_USER_PASSWORD,
    assign_reviewer,
    assign_to_order,
    auth_headers,
    create_branch,
    create_order,
    create_report,
    create_sample,
    create_tenant,
    create_user,
)
from .numbering_support import new_client, run_parallel

CONFIRM = {"password": TEST_USER_PASSWORD}
WRONG = "wrong-amendment-confirmation-4c1"


# ---------------------------------------------------------------------------
# Setup: a report published through the real signing route
# ---------------------------------------------------------------------------


@pytest.fixture
def lab(session: Session, client, stub_pdf_render):
    tenant = create_tenant(session)
    branch = create_branch(session, tenant)
    order = create_order(session, tenant, branch, order_code="ORD-AM-1")
    create_sample(session, tenant, branch, order, state=SampleState.READY)
    author = create_user(session, tenant, email="author@am.example", roles=("pathologist",))
    reviewer = create_user(session, tenant, email="rev@am.example", roles=("reviewer",))
    admin = create_user(session, tenant, email="admin@am.example", roles=("admin",))
    assignee = create_user(session, tenant, email="tech@am.example", roles=("lab_tech",))
    report, v1 = create_report(
        session, tenant, branch, order, status=ReportStatus.APPROVED,
        pdf_generation_status="READY", created_by=author, authored_by=author,
    )
    review = assign_reviewer(session, order, reviewer, report=report, status=ReviewStatus.APPROVED)
    assign_to_order(session, tenant, order, assignee)

    stub_pdf_render.succeed(make_pdf_bytes(1))
    resp = client.post(f"/api/v1/reports/{report.id}/sign-and-publish", json=CONFIRM, headers=auth_headers(reviewer))
    assert resp.status_code == 200, resp.text
    session.expire_all()
    return {
        "tenant": tenant, "branch": branch, "order": order, "report": report, "v1": v1,
        "author": author, "reviewer": reviewer, "admin": admin, "assignee": assignee, "review": review,
    }


def _url(report, suffix=""):
    return f"/api/v1/reports/{report.id}{suffix}"


def _open(client, lab, actor=None, **body):
    payload = {"category": "CORRECTION", "reason": "Diagnóstico con error de lateralidad", **CONFIRM, **body}
    return client.post(_url(lab["report"], "/amendments"), json=payload, headers=auth_headers(actor or lab["reviewer"]))


def _save(client, lab, note="Se corrige la lateralidad: izquierda en lugar de derecha.", **extra):
    body = {
        "base": {}, "sections": {}, "base_order": [], "section_order": [],
        "amendment": {"change_note": note, "amendment_no": 99, "reason": "forged"},
        **extra,
    }
    return client.post(
        _url(lab["report"], "/new_version"),
        json={
            "tenant_id": str(lab["tenant"].id), "branch_id": str(lab["branch"].id),
            "order_id": str(lab["order"].id), "title": "Reporte", "report": body,
            "created_by": str(lab["author"].id),
        },
        headers=auth_headers(lab["author"]),
    )


def _submit(client, lab):
    return client.post(_url(lab["report"], "/submit"), json={}, headers=auth_headers(lab["author"]))


def _approve(client, lab):
    return client.post(_url(lab["report"], "/approve"), json={}, headers=auth_headers(lab["reviewer"]))


def _sign(client, lab, body=None, route="/sign-and-publish"):
    return client.post(_url(lab["report"], route), json=body or CONFIRM, headers=auth_headers(lab["reviewer"]))


def _cancel(client, lab, amendment_id, actor=None, **body):
    payload = {"reason": "Se abrió por error", **CONFIRM, **body}
    return client.post(
        _url(lab["report"], f"/amendments/{amendment_id}/cancel"), json=payload,
        headers=auth_headers(actor or lab["reviewer"]),
    )


def _drive_to_approved(client, lab):
    assert _save(client, lab).status_code == 200
    assert _submit(client, lab).status_code == 200
    assert _approve(client, lab).status_code == 200


def _events(session, order_id, action):
    session.expire_all()
    return [
        e for e in session.exec(select(OrderEvent).where(OrderEvent.order_id == order_id)).all()
        if (e.event_metadata or {}).get("action") == action
    ]


def _audit(session, report_id, action):
    session.expire_all()
    return list(session.exec(select(AuditLog).where(AuditLog.entity_id == report_id, AuditLog.action == action)).all())


def _version(session, report, version_no):
    session.expire_all()
    return session.exec(
        select(ReportVersion).where(ReportVersion.report_id == report.id, ReportVersion.version_no == version_no)
    ).one()


def _body(session, version):
    storage = session.get(StorageObject, version.json_storage_id)
    return json.loads(FakeS3Service.store[storage.object_key].decode("utf-8"))


def _fingerprint(session, version):
    """Everything about a signed emission that must never change."""
    session.expire_all()
    v = session.get(ReportVersion, version.id)
    json_obj = session.get(StorageObject, v.json_storage_id)
    pdf_obj = session.get(StorageObject, v.pdf_storage_id)
    return (
        v.signed_by, v.signed_at, v.pdf_sha256, v.pdf_storage_id, v.json_storage_id,
        v.pdf_generation_status, FakeS3Service.store[json_obj.object_key],
        FakeS3Service.store[pdf_obj.object_key], json_obj.size_bytes, pdf_obj.size_bytes,
    )


def _state(session, lab):
    session.expire_all()
    return (
        sorted(session.execute(text(f"SELECT row_to_json(t)::text FROM {t} t")).scalars().all())
        for t in ("report", "report_version", "report_amendment", "report_review", "order_event",
                  "storage_object", "notification", '"order"')
    )


def _snapshot(session, lab):
    return [list(rows) for rows in _state(session, lab)], sorted(
        session.execute(text("SELECT row_to_json(t)::text FROM audit_log t WHERE action <> :a"),
                        {"a": PASSWORD_CONFIRMATION_FAILED_ACTION}).scalars().all()
    ), dict(FakeS3Service.store)


# ---------------------------------------------------------------------------
# The happy path: reopen for amendment -> review -> republication
# ---------------------------------------------------------------------------


class TestAmendmentLifecycle:
    def test_reopen_for_amendment_preserves_and_keeps_delivering_the_original(self, client, session, lab):
        report, v1 = lab["report"], lab["v1"]
        original = _fingerprint(session, v1)

        resp = _open(client, lab, category="SUPPLEMENT", reason="Resultado de IHC posterior")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["status"] == "DRAFT"
        assert payload["amendment"]["amendment_no"] == 1
        assert payload["amendment"]["state"] == "OPEN"
        assert payload["amendment"]["category"] == "SUPPLEMENT"
        assert payload["amendment"]["source_version_no"] == 1

        session.expire_all()
        assert session.get(Report, report.id).status == ReportStatus.DRAFT
        v2 = _version(session, report, 2)
        amendment = session.exec(select(ReportAmendment).where(ReportAmendment.report_id == report.id)).one()
        assert v2.is_current and not _version(session, report, 1).is_current
        assert v2.amendment_id == amendment.id and v2.signed_at is None and v2.pdf_storage_id is None
        # D1: a complete copy of the delivered content, identified (D2), with no
        # inherited signature image or delivery date.
        b1, b2 = _body(session, v1), _body(session, v2)
        assert b2["rendering_snapshot"] == b1["rendering_snapshot"]
        assert b2["sections"] == b1["sections"]
        assert b2["amendment"] == {
            "amendment_no": 1, "category": "SUPPLEMENT", "reason": "Resultado de IHC posterior",
            "amends_version_no": 1, "amends_signed_at": v1.signed_at.isoformat(), "change_note": "",
        }
        assert "signature_url" not in (b2.get("signatureMetadata") or {})
        # The original is untouched, byte for byte, and is still what is delivered.
        assert _fingerprint(session, v1) == original
        latest = client.get(_url(report, "/pdf"), headers=auth_headers(lab["author"])).json()
        assert latest["version_no"] == 1
        # D6: the order left its completed state.
        assert session.get(Order, lab["order"].id).status == OrderStatus.DIAGNOSIS
        # Exactly one audit row and one timeline row, correlated.
        audit = _audit(session, report.id, "REPORT.REOPEN_FOR_AMENDMENT")
        events = _events(session, lab["order"].id, "REPORT_REOPENED_FOR_AMENDMENT")
        assert len(audit) == 1 and len(events) == 1
        meta = events[0].event_metadata
        assert events[0].event_type == EventType.STATUS_CHANGED
        assert meta["amendment_id"] == str(amendment.id) and meta["amendment_no"] == 1
        assert meta["amends_version_id"] == str(v1.id) and meta["amends_version_no"] == 1
        assert meta["report_version_id"] == str(v2.id) and meta["version_no"] == 2
        assert meta["from_status"] == "PUBLISHED" and meta["to_status"] == "DRAFT"
        assert meta["reason"] == "Resultado de IHC posterior" and meta["category"] == "SUPPLEMENT"
        assert meta["reopened_by"] == str(lab["reviewer"].id)
        assert audit[0].new_values["amendment_id"] == str(amendment.id)
        assert audit[0].new_values["password_confirmed"] is True
        assert audit[0].actor_user_id == lab["reviewer"].id

    def test_full_cycle_republishes_a_superseding_emission(self, client, session, lab, stub_pdf_render):
        report, v1 = lab["report"], lab["v1"]
        original = _fingerprint(session, v1)
        amendment_id = _open(client, lab).json()["amendment"]["id"]

        # The author edits; only the change note is theirs to write (D2).
        saved = _save(client, lab)
        assert saved.status_code == 200, saved.text
        v3 = _version(session, report, 3)
        block = _body(session, v3)["amendment"]
        assert block["change_note"].startswith("Se corrige la lateralidad")
        assert block["amendment_no"] == 1 and block["reason"] == "Diagnóstico con error de lateralidad"
        assert v3.amendment_id is not None

        assert _submit(client, lab).status_code == 200
        assert _approve(client, lab).status_code == 200
        # Delivery is still the original while the amendment is approved but unsigned.
        assert client.get(_url(report, "/pdf"), headers=auth_headers(lab["author"])).json()["version_no"] == 1

        stub_pdf_render.succeed(make_pdf_bytes(2))
        signed = _sign(client, lab, {**CONFIRM, "changelog": "Enmienda firmada"})
        assert signed.status_code == 200, signed.text
        assert signed.json()["version_no"] == 3

        session.expire_all()
        assert session.get(Report, report.id).status == ReportStatus.PUBLISHED
        amendment = session.get(ReportAmendment, uuid.UUID(amendment_id))
        assert amendment.state == "PUBLISHED" and amendment.published_version_id == v3.id
        assert _fingerprint(session, v1) == original
        # The amended emission is now delivered (D1/D3) and the order is complete again (D6).
        assert client.get(_url(report, "/pdf"), headers=auth_headers(lab["author"])).json()["version_no"] == 3
        assert session.get(Order, lab["order"].id).status in (OrderStatus.CLOSED, OrderStatus.RELEASED)

        events = _events(session, lab["order"].id, "REPORT_REPUBLISHED")
        audit = [a for a in _audit(session, report.id, "REPORT.SIGN_AND_PUBLISH")
                 if a.new_values.get("publication_kind") == "AMENDMENT"]
        assert len(events) == 1 and len(audit) == 1
        meta = events[0].event_metadata
        assert events[0].event_type == EventType.REPORT_PUBLISHED
        assert meta["publication_kind"] == "AMENDMENT"
        assert meta["amendment_id"] == amendment_id and meta["amendment_no"] == 1
        assert meta["amends_version_id"] == str(v1.id) and meta["amends_version_no"] == 1
        assert meta["report_version_id"] == str(v3.id) and meta["version_no"] == 3
        assert meta["reason"] == "Diagnóstico con error de lateralidad"
        assert meta["from_status"] == "APPROVED" and meta["to_status"] == "PUBLISHED"
        assert meta["signer_id"] == str(lab["reviewer"].id)
        assert audit[0].new_values["amends_version_id"] == str(v1.id)
        # Exactly one initial publication remains in the history, not rewritten.
        assert len(_events(session, lab["order"].id, "REPORT_PUBLISHED")) == 1

        # The intervening transitions are linked to the amendment.
        for event_type in (EventType.REPORT_VERSION_CREATED, EventType.REPORT_SUBMITTED, EventType.REPORT_APPROVED):
            rows = [e for e in session.exec(select(OrderEvent).where(
                OrderEvent.order_id == lab["order"].id, OrderEvent.event_type == event_type)).all()
                if (e.event_metadata or {}).get("amendment_id") == amendment_id]
            assert len(rows) == 1, event_type

        # D4: in-app notification to the publication recipients, no email delivery.
        republished = session.exec(select(Notification).where(Notification.type == "REPORT_REPUBLISHED")).all()
        assert len(republished) == 1
        assert session.exec(select(NotificationDelivery).where(
            NotificationDelivery.notification_id == republished[0].id)).all() == []

    def test_the_change_note_is_required_before_review_and_signing(self, client, session, lab):
        _open(client, lab)
        assert _save(client, lab, note="   ").status_code == 200
        resp = _submit(client, lab)
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "amendment_change_note_required"
        assert session.get(Report, lab["report"].id).status == ReportStatus.DRAFT

    def test_ordinary_reopen_inside_an_amendment_is_allowed_and_correlated(self, client, session, lab):
        amendment_id = _open(client, lab).json()["amendment"]["id"]
        _drive_to_approved(client, lab)
        resp = client.post(_url(lab["report"], "/reopen"), json={**CONFIRM, "changelog": "Falta un dato"},
                           headers=auth_headers(lab["reviewer"]))
        assert resp.status_code == 200, resp.text
        reopen = _events(session, lab["order"].id, "REPORT_REOPENED")
        assert len(reopen) == 1 and reopen[0].event_metadata["amendment_id"] == amendment_id

    def test_a_worklist_decision_inside_an_amendment_is_correlated(self, client, session, lab):
        amendment_id = _open(client, lab).json()["amendment"]["id"]
        assert _save(client, lab).status_code == 200
        assert _submit(client, lab).status_code == 200
        review = session.exec(select(ReportReview).where(ReportReview.order_id == lab["order"].id)).one()
        resp = client.post(f"/api/v1/report-reviews/{review.id}/decision",
                           json={"decision": "approved"}, headers=auth_headers(lab["reviewer"]))
        assert resp.status_code == 200, resp.text
        approved = [e for e in session.exec(select(OrderEvent).where(
            OrderEvent.order_id == lab["order"].id, OrderEvent.event_type == EventType.REPORT_APPROVED)).all()
            if (e.event_metadata or {}).get("amendment_id") == amendment_id]
        assert len(approved) == 1
        audit = session.exec(select(AuditLog).where(AuditLog.action == "REVIEW.APPROVED")).all()
        assert audit[-1].new_values["amendment_id"] == amendment_id

    def test_successive_amendments_amend_the_delivered_emission(self, client, session, lab, stub_pdf_render):
        _open(client, lab)
        _drive_to_approved(client, lab)
        assert _sign(client, lab).status_code == 200
        second = _open(client, lab, reason="Segunda corrección")
        assert second.status_code == 200, second.text
        assert second.json()["amendment"]["amendment_no"] == 2
        assert second.json()["amendment"]["source_version_no"] == 3

    def test_emission_history_lists_signed_emissions_and_amendments(self, client, session, lab, stub_pdf_render):
        _open(client, lab)
        _drive_to_approved(client, lab)
        _sign(client, lab)
        history = client.get(_url(lab["report"], "/emissions"), headers=auth_headers(lab["assignee"]))
        assert history.status_code == 200, history.text
        emissions = history.json()["emissions"]
        assert [(e["version_no"], e["is_delivered"], e["amendment_no"]) for e in emissions] == [(3, True, 1), (1, False, None)]
        assert all(e["pdf_available"] for e in emissions)
        assert history.json()["amendments"][0]["state"] == "PUBLISHED"
        # Each signed emission stays downloadable for authorized readers.
        for version_no in (1, 3):
            assert client.get(_url(lab["report"], f"/versions/{version_no}/pdf"),
                              headers=auth_headers(lab["assignee"])).status_code == 200

    def test_the_official_render_envelope_carries_the_identification(self, client, session, lab):
        """The headless renderer of the official PDF reads this envelope; it
        must build (a 500 here times out every PDF) and carry the frozen
        identification block of the amendment draft (D2)."""
        from app.core.security import create_render_token

        _open(client, lab)
        _save(client, lab, note="Se agrega IHC.")
        v3 = _version(session, lab["report"], 3)
        token = create_render_token(str(v3.id), str(lab["tenant"].id), 90)
        resp = client.get(
            f"/api/v1/reports/internal/render-data/{lab['report'].id}/3",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["report"]["amendment"]["amendment_no"] == 1
        assert body["report"]["amendment"]["change_note"] == "Se agrega IHC."
        assert body["open_amendment"]["amendment_no"] == 1

    def test_detail_exposes_the_open_amendment_and_the_delivered_emission(self, client, session, lab):
        _open(client, lab)
        full = client.get(_url(lab["report"], "/full"), headers=auth_headers(lab["author"])).json()["report"]
        assert full["status"] == "DRAFT" and full["version_no"] == 2
        assert full["open_amendment"]["amendment_no"] == 1
        assert full["delivered_emission"]["version_no"] == 1
        assert full["report"]["amendment"]["amendment_no"] == 1
        order_full = client.get(f"/api/v1/laboratory/orders/{lab['order'].id}/full", headers=auth_headers(lab["author"]))
        assert order_full.status_code == 200, order_full.text
        assert order_full.json()["report"]["open_amendment_no"] == 1


# ---------------------------------------------------------------------------
# Cancellation (D3)
# ---------------------------------------------------------------------------


class TestCancellation:
    def test_cancel_restores_the_delivered_state_and_records_it(self, client, session, lab):
        report, v1 = lab["report"], lab["v1"]
        original = _fingerprint(session, v1)
        amendment_id = _open(client, lab).json()["amendment"]["id"]
        _save(client, lab)
        assert _submit(client, lab).status_code == 200

        resp = _cancel(client, lab, amendment_id, reason="La IHC se reportará en otra orden")
        assert resp.status_code == 200, resp.text
        session.expire_all()
        assert session.get(Report, report.id).status == ReportStatus.PUBLISHED
        assert _version(session, report, 1).is_current
        assert not _version(session, report, 3).is_current
        assert _version(session, report, 3).amendment_id is not None  # drafts kept as history
        assert session.get(ReportAmendment, uuid.UUID(amendment_id)).state == "CANCELLED"
        assert _fingerprint(session, v1) == original
        assert session.get(ReportReview, lab["review"].id).status == ReviewStatus.APPROVED
        assert session.get(Order, lab["order"].id).status in (OrderStatus.CLOSED, OrderStatus.RELEASED)
        assert client.get(_url(report, "/pdf"), headers=auth_headers(lab["author"])).json()["version_no"] == 1

        events = _events(session, lab["order"].id, "REPORT_AMENDMENT_CANCELLED")
        audit = _audit(session, report.id, "REPORT.AMENDMENT_CANCEL")
        assert len(events) == 1 and len(audit) == 1
        meta = events[0].event_metadata
        assert meta["reason"] == "La IHC se reportará en otra orden"
        assert meta["amendment_id"] == amendment_id and meta["report_version_id"] == str(v1.id)
        assert meta["from_status"] == "IN_REVIEW" and meta["to_status"] == "PUBLISHED"
        assert meta["cancelled_by"] == str(lab["reviewer"].id)

        again = _cancel(client, lab, amendment_id)
        assert again.status_code == 400
        assert len(_events(session, lab["order"].id, "REPORT_AMENDMENT_CANCELLED")) == 1
        # A new amendment can be opened afterwards and gets the next number.
        assert _open(client, lab).json()["amendment"]["amendment_no"] == 2

    def test_cancel_is_refused_while_the_amendment_is_being_signed(self, client, session, lab):
        from datetime import datetime

        amendment_id = _open(client, lab).json()["amendment"]["id"]
        _drive_to_approved(client, lab)
        current = _version(session, lab["report"], 3)
        current.publish_started_at = datetime.utcnow()
        current.publish_started_by = lab["reviewer"].id
        session.add(current)
        session.commit()
        before = _snapshot(session, lab)
        resp = _cancel(client, lab, amendment_id)
        assert resp.status_code == 409 and resp.json()["detail"]["code"] == "amendment_publish_in_progress"
        assert _snapshot(session, lab) == before

    def test_a_stale_amendment_id_is_refused(self, client, session, lab):
        _open(client, lab)
        resp = _cancel(client, lab, str(uuid.uuid4()))
        assert resp.status_code == 409 and resp.json()["detail"]["code"] == "amendment_not_open"


# ---------------------------------------------------------------------------
# Refusals change nothing and record no success
# ---------------------------------------------------------------------------


class TestRefusals:
    @pytest.mark.parametrize("password,code", [(None, "password_confirmation_required"), (WRONG, "password_confirmation_invalid")])
    def test_opening_without_the_actors_password(self, client, session, lab, password, code):
        before = _snapshot(session, lab)
        resp = _open(client, lab, password=password)
        assert resp.status_code == 403 and resp.json()["detail"]["code"] == code
        assert _snapshot(session, lab) == before

    @pytest.mark.parametrize("password,code", [(None, "password_confirmation_required"), (WRONG, "password_confirmation_invalid")])
    def test_cancelling_without_the_actors_password(self, client, session, lab, password, code):
        amendment_id = _open(client, lab).json()["amendment"]["id"]
        before = _snapshot(session, lab)
        resp = _cancel(client, lab, amendment_id, password=password)
        assert resp.status_code == 403 and resp.json()["detail"]["code"] == code
        assert _snapshot(session, lab) == before

    @pytest.mark.parametrize("role", ["pathologist", "lab_tech", "unassigned_reviewer"])
    def test_non_reopening_actors_cannot_open(self, client, session, lab, role):
        roles = ("reviewer",) if role == "unassigned_reviewer" else (role,)
        user = create_user(session, lab["tenant"], email=f"{role}-{uuid.uuid4().hex[:5]}@am.example", roles=roles)
        before = _snapshot(session, lab)
        resp = _open(client, lab, actor=user, password=WRONG)
        assert resp.status_code == 403
        assert _snapshot(session, lab) == before
        assert session.exec(select(AuditLog).where(AuditLog.action == PASSWORD_CONFIRMATION_FAILED_ACTION)).all() == []

    def test_foreign_tenant_cannot_open(self, client, session, lab):
        other = create_tenant(session, name="Otro laboratorio")
        intruder = create_user(session, other, email="intruder@other.example", roles=("admin",))
        resp = _open(client, lab, actor=intruder)
        assert resp.status_code == 403

    def test_admin_opens_with_own_password_but_cannot_sign(self, client, session, lab, stub_pdf_render):
        assert _open(client, lab, actor=lab["admin"]).status_code == 200
        _drive_to_approved(client, lab)
        resp = client.post(_url(lab["report"], "/sign-and-publish"), json=CONFIRM, headers=auth_headers(lab["admin"]))
        assert resp.status_code == 403
        assert _events(session, lab["order"].id, "REPORT_REPUBLISHED") == []

    def test_unpublished_and_retracted_reports_are_not_amendable(self, client, session, lab):
        report = session.get(Report, lab["report"].id)
        report.status = ReportStatus.RETRACTED
        session.add(report)
        session.commit()
        assert _open(client, lab).status_code == 400
        order2 = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-AM-2")
        draft, _ = create_report(session, lab["tenant"], lab["branch"], order2, status=ReportStatus.DRAFT)
        assign_reviewer(session, order2, lab["reviewer"], report=draft)
        resp = client.post(f"/api/v1/reports/{draft.id}/amendments",
                           json={"category": "CORRECTION", "reason": "Motivo válido", **CONFIRM},
                           headers=auth_headers(lab["reviewer"]))
        assert resp.status_code == 400

    def test_legacy_originals_are_not_amendable(self, client, session, lab):
        # D5. A legacy emission cannot be produced through today's API, so the
        # signed version is marked legacy directly.
        v1 = session.get(ReportVersion, lab["v1"].id)
        v1.schema_version = None
        session.add(v1)
        session.commit()
        before = _snapshot(session, lab)
        resp = _open(client, lab)
        assert resp.status_code == 409 and resp.json()["detail"]["code"] == "amendment_legacy_original"
        assert _snapshot(session, lab) == before

    def test_a_reason_is_required(self, client, session, lab):
        assert _open(client, lab, reason="  ").status_code == 422
        assert _open(client, lab, category="ADDENDUM").status_code == 422

    def test_a_forged_identification_outside_an_amendment_is_stripped(self, client, session, lab):
        order2 = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-AM-3")
        draft, _ = create_report(session, lab["tenant"], lab["branch"], order2, status=ReportStatus.DRAFT)
        resp = client.post(
            f"/api/v1/reports/{draft.id}/new_version",
            json={"tenant_id": str(lab["tenant"].id), "branch_id": str(lab["branch"].id), "order_id": str(order2.id),
                  "report": {"base": {}, "sections": {}, "amendment": {"amendment_no": 7, "change_note": "x"}},
                  "created_by": str(lab["author"].id)},
            headers=auth_headers(lab["author"]),
        )
        assert resp.status_code == 200, resp.text
        assert "amendment" not in _body(session, _version(session, draft, 2))

    def test_failed_republication_records_nothing_and_keeps_delivering_the_original(
        self, client, session, lab, stub_pdf_render
    ):
        from app.services.report_pdf_generation import ReportPdfGenerationError

        _open(client, lab)
        _drive_to_approved(client, lab)
        stub_pdf_render.fail(ReportPdfGenerationError("RENDER_FAILED", "synthetic"))
        assert _sign(client, lab).status_code == 422
        assert _events(session, lab["order"].id, "REPORT_REPUBLISHED") == []
        assert session.exec(select(ReportAmendment)).one().state == "OPEN"
        assert client.get(_url(lab["report"], "/pdf"), headers=auth_headers(lab["author"])).json()["version_no"] == 1
        # Retryable, and then exactly one republication.
        stub_pdf_render.succeed(make_pdf_bytes(1))
        assert _sign(client, lab).status_code == 200
        assert _sign(client, lab).status_code == 400
        assert len(_events(session, lab["order"].id, "REPORT_REPUBLISHED")) == 1

    def test_wrong_password_on_republication_records_nothing(self, client, session, lab, stub_pdf_render):
        _open(client, lab)
        _drive_to_approved(client, lab)
        resp = _sign(client, lab, {"password": WRONG})
        assert resp.status_code == 403
        assert _events(session, lab["order"].id, "REPORT_REPUBLISHED") == []
        assert session.get(Report, lab["report"].id).status == ReportStatus.APPROVED

    def test_the_legacy_sign_route_republishes_the_same_way(self, client, session, lab, stub_pdf_render):
        _open(client, lab)
        _drive_to_approved(client, lab)
        assert client.post(_url(lab["report"], "/versions/3/generate-pdf"), headers=auth_headers(lab["author"])).status_code == 200
        resp = _sign(client, lab, route="/sign")
        assert resp.status_code == 200, resp.text
        events = _events(session, lab["order"].id, "REPORT_REPUBLISHED")
        assert len(events) == 1
        assert [a for a in _audit(session, lab["report"].id, "REPORT.SIGN")
                if a.new_values.get("publication_kind") == "AMENDMENT"]


# ---------------------------------------------------------------------------
# Delivery to portals (D3)
# ---------------------------------------------------------------------------


class TestPortalDelivery:
    def _physician(self, session, lab):
        physician = create_user(session, lab["tenant"], email="medico@am.example", roles=("physician",))
        order = session.get(Order, lab["order"].id)
        order.requested_by = physician.email
        session.add(order)
        session.commit()
        return physician

    def test_portal_keeps_the_signed_emission_and_then_shows_the_supersession(self, client, session, lab, stub_pdf_render):
        physician = self._physician(session, lab)
        url = f"/api/v1/portal/physician/orders/{lab['order'].id}/report"

        v1 = _version(session, lab["report"], 1)
        v1_pdf_key = session.get(StorageObject, v1.pdf_storage_id).object_key
        _open(client, lab)
        during = client.get(url, headers=auth_headers(physician))
        assert during.status_code == 200, during.text
        assert during.json()["status"] == "PUBLISHED" and v1_pdf_key in during.json()["pdf_url"]
        assert during.json()["supersedes"] is None
        assert "version_no" not in during.json()
        listing = client.get("/api/v1/portal/physician/orders", headers=auth_headers(physician)).json()
        assert listing[0]["report_status"] == "PUBLISHED"  # the amendment is internal

        _drive_to_approved(client, lab)
        _sign(client, lab)
        after = client.get(url, headers=auth_headers(physician)).json()
        v3 = _version(session, lab["report"], 3)
        assert session.get(StorageObject, v3.pdf_storage_id).object_key in after["pdf_url"]
        assert after["supersedes"] == {"amendment_no": 1, "supersedes_signed_at": v1.signed_at.isoformat()}

    def test_a_never_signed_report_is_still_not_available(self, client, session, lab):
        physician = self._physician(session, lab)
        report = session.get(Report, lab["report"].id)
        report.status = ReportStatus.RETRACTED
        session.add(report)
        session.commit()
        resp = client.get(f"/api/v1/portal/physician/orders/{lab['order'].id}/report", headers=auth_headers(physician))
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Concurrency: exactly one transition and one event
# ---------------------------------------------------------------------------


@pytest.fixture
def parallel(lab, engine):
    """Per-request sessions for concurrent requests. `lab` publishes through the
    shared-session `client`, whose override must be replaced afterwards:
    threads may never share one session."""
    from app.core.db import get_session
    from app.main import app

    def override():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = override
    yield
    # The `client` fixture clears every override at teardown.


class TestConcurrency:
    def test_concurrent_openings_create_one_amendment(self, parallel, session, lab):
        headers = auth_headers(lab["reviewer"])
        url = _url(lab["report"], "/amendments")
        payload = {"category": "CORRECTION", "reason": "Motivo concurrente", **CONFIRM}
        results = run_parallel([lambda c: c.post(url, json=payload, headers=headers) for _ in range(4)])
        codes = sorted(getattr(r, "status_code", r) for r in results)
        assert codes == [200, 400, 400, 400], [getattr(r, "text", r) for r in results]
        session.expire_all()
        assert len(session.exec(select(ReportAmendment)).all()) == 1
        assert len(_events(session, lab["order"].id, "REPORT_REOPENED_FOR_AMENDMENT")) == 1
        assert len(_audit(session, lab["report"].id, "REPORT.REOPEN_FOR_AMENDMENT")) == 1

    def test_concurrent_cancellations_record_one(self, parallel, session, lab):
        amendment_id = new_client().post(
            _url(lab["report"], "/amendments"),
            json={"category": "CORRECTION", "reason": "Motivo", **CONFIRM}, headers=auth_headers(lab["reviewer"]),
        ).json()["amendment"]["id"]
        headers = auth_headers(lab["reviewer"])
        url = _url(lab["report"], f"/amendments/{amendment_id}/cancel")
        results = run_parallel([lambda c: c.post(url, json={"reason": "Cancelación", **CONFIRM}, headers=headers) for _ in range(3)])
        codes = sorted(getattr(r, "status_code", r) for r in results)
        assert codes == [200, 400, 400], [getattr(r, "text", r) for r in results]
        assert len(_events(session, lab["order"].id, "REPORT_AMENDMENT_CANCELLED")) == 1
