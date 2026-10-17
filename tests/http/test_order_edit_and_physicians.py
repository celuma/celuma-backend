"""Basic order edits (#9) and several requesting physicians + expediente (#16), over real HTTP + PostgreSQL.

Contract: `celuma-engineering/specs/laboratory/order-sample-management.md`.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.models.enums import EventType, OrderStatus, ReportStatus, ReviewStatus
from app.services import report_metadata

from .factories import auth_headers, create_branch, create_tenant, create_user
from .management_support import (
    LAB, add_event, count, decide_review, make_report, physician, register, set_status, sign_version,
)
from .numbering_support import Lab, freeze


@pytest.fixture
def lab(session, monkeypatch):
    freeze(monkeypatch)
    return Lab(session, "Alfa", sample_types=("BX", "TJ"))


@pytest.fixture
def docs(session, lab):
    return [physician(session, lab, f"MS{i}", n, "Solicitante") for i, n in enumerate(("Ana", "Beto", "Carla"), 1)]


def patch(client, lab, order_id, body, headers=None):
    return client.patch(f"{LAB}/orders/{order_id}", json=body, headers=headers or lab.headers)


def detail(client, lab, order_id):
    res = client.get(f"{LAB}/orders/{order_id}", headers=lab.headers)
    assert res.status_code == 200, res.text
    return res.json()


# ---------------------------------------------------------------- registration with physicians / expediente


class TestRegistration:
    def test_several_physicians_no_primary_are_persisted_in_order(self, client, session, lab, docs):
        ids = [str(docs[2].id), str(docs[0].id)]
        reg = register(client, lab, requesting_physician_ids=ids, requested_by=None)
        body = detail(client, lab, reg["order_id"])
        assert [p["id"] for p in body["requesting_physicians"]] == ids
        # no primary: the legacy single fields do not pick one of two
        assert body["requesting_physician"] is None and body["requesting_physician_id"] is None
        assert body["requested_by"] == "Carla Solicitante; Ana Solicitante"

    def test_single_physician_keeps_the_legacy_fields_coherent(self, client, session, lab, docs):
        reg = register(client, lab, requesting_physician_id=str(docs[1].id), requested_by=None)
        body = detail(client, lab, reg["order_id"])
        assert [p["id"] for p in body["requesting_physicians"]] == [str(docs[1].id)]
        assert body["requesting_physician_id"] == str(docs[1].id)
        assert body["requesting_physician"]["id"] == str(docs[1].id)

    def test_expediente_alone_alongside_physicians_and_leading_zeroes(self, client, session, lab, docs):
        only = register(client, lab, expediente="  00012345 ")
        body = detail(client, lab, only["order_id"])
        assert body["expediente"] == "00012345" and body["requesting_physicians"] == []
        both = register(client, lab, expediente="000-9", requesting_physician_ids=[str(docs[0].id)], requested_by=None)
        body = detail(client, lab, both["order_id"])
        assert body["expediente"] == "000-9" and len(body["requesting_physicians"]) == 1
        assert body["requested_by"] == "Ana Solicitante"  # the expediente is never mixed into the name

    @pytest.mark.parametrize("bad", [
        {"requesting_physician_ids": ["x"] * 2},            # repeated
        {"requesting_physician_ids": [" "]},                # blank
        {"requesting_physician_ids": [str(i) for i in range(11)]},  # above the engineering bound
        {"expediente": "x" * 101},
        {"expediente": "a\nb"},
    ])
    def test_invalid_physician_list_or_expediente_is_422(self, client, lab, bad):
        res = client.post("/api/v1/laboratory/orders/unified", json=lab.unified(**bad), headers=lab.headers)
        assert res.status_code == 422, res.text

    def test_foreign_and_inactive_physicians_are_refused(self, client, session, lab, docs):
        other = Lab(session, "Beta")
        foreign = physician(session, other, "FX1")
        inactive = physician(session, lab, "MS9", active=False)
        res = client.post("/api/v1/laboratory/orders/unified", json=lab.unified(requesting_physician_ids=[str(foreign.id)]),
                          headers=lab.headers)
        assert res.status_code == 403
        res = client.post("/api/v1/laboratory/orders/unified", json=lab.unified(requesting_physician_ids=[str(inactive.id)]),
                          headers=lab.headers)
        assert res.status_code == 400

    def test_deprecated_single_field_must_not_contradict_the_list(self, client, lab, docs):
        res = client.post("/api/v1/laboratory/orders/unified", headers=lab.headers, json=lab.unified(
            requesting_physician_id=str(docs[0].id), requesting_physician_ids=[str(docs[1].id)]))
        assert res.status_code == 422 and res.json()["detail"]["code"] == "requesting_physician_conflict"

    def test_referencing_a_physician_grants_nothing_and_sends_nothing(self, client, session, lab, docs):
        before = count(session, "SELECT count(*) FROM notification")
        reg = register(client, lab, requesting_physician_ids=[str(docs[0].id)])
        assert count(session, "SELECT count(*) FROM notification") == before
        # the physician portal keys on the free-text requester == the caller's e-mail, never on the catalog link
        portal_user = create_user(session, lab.tenant, email="ana@lab.invalid", roles=("superuser",))
        res = client.get(f"/api/v1/portal/physician/orders/{reg['order_id']}/report", headers=auth_headers(portal_user))
        assert res.status_code == 403


# ---------------------------------------------------------------- order edit


class TestEdit:
    def test_physicians_expediente_and_notes_persist_and_show_after_reopening(self, client, session, lab, docs):
        reg = register(client, lab)
        res = patch(client, lab, reg["order_id"], {
            "requesting_physician_ids": [str(docs[1].id), str(docs[2].id)], "expediente": "0042", "notes": "revisar tinción",
        })
        assert res.status_code == 200, res.text
        body = detail(client, lab, reg["order_id"])
        assert [p["id"] for p in body["requesting_physicians"]] == [str(docs[1].id), str(docs[2].id)]
        assert body["expediente"] == "0042" and body["notes"] == "revisar tinción"
        assert body["requesting_physician_id"] is None
        # the lists agree (filter by either physician, search by name or expediente, no primary)
        for doc in (docs[1], docs[2]):
            listed = client.get(f"{LAB}/orders/", params={"requesting_physician_id": str(doc.id)}, headers=lab.headers).json()
            assert [o["id"] for o in listed["orders"]] == [reg["order_id"]]
        for q in ("Beto", "0042"):
            listed = client.get(f"{LAB}/orders/", params={"q": q}, headers=lab.headers).json()
            assert [o["id"] for o in listed["orders"]] == [reg["order_id"]], q
            assert [p["id"] for p in listed["orders"][0]["requesting_physicians"]] == [str(docs[1].id), str(docs[2].id)]
        assert client.get(f"{LAB}/orders/", params={"requesting_physician_id": str(docs[0].id)},
                          headers=lab.headers).json()["orders"] == []
        options = client.get(f"{LAB}/orders/filter-options", headers=lab.headers).json()
        assert {o["value"] for o in options["requesting_physicians"]} == {str(docs[1].id), str(docs[2].id)}

    def test_expediente_can_be_cleared_and_unchanged_edits_write_nothing(self, client, session, lab, docs):
        reg = register(client, lab, expediente="77")
        assert patch(client, lab, reg["order_id"], {"expediente": None}).json()["expediente"] is None
        events = count(session, "SELECT count(*) FROM order_event WHERE event_type = 'ORDER_UPDATED'")
        audits = count(session, "SELECT count(*) FROM audit_log WHERE action = 'ORDER.UPDATED'")
        assert patch(client, lab, reg["order_id"], {"expediente": None, "notes": None}).status_code == 200
        assert count(session, "SELECT count(*) FROM order_event WHERE event_type = 'ORDER_UPDATED'") == events
        assert count(session, "SELECT count(*) FROM audit_log WHERE action = 'ORDER.UPDATED'") == audits

    def test_edit_is_audited_with_old_and_new_values(self, client, session, lab, docs):
        reg = register(client, lab, requesting_physician_ids=[str(docs[0].id)], expediente="1")
        patch(client, lab, reg["order_id"], {"expediente": "2", "requesting_physician_ids": [str(docs[1].id)]})
        row = session.execute(text("SELECT old_values, new_values, actor_user_id FROM audit_log WHERE action = 'ORDER.UPDATED'")).one()
        assert row.old_values == {"expediente": "1", "requesting_physician_ids": [str(docs[0].id)]}
        assert row.new_values == {"expediente": "2", "requesting_physician_ids": [str(docs[1].id)]}
        assert str(row.actor_user_id) == str(lab.user.id)
        event = session.execute(text(
            "SELECT event_metadata FROM order_event WHERE event_type = 'ORDER_UPDATED'")).scalar_one()
        assert event["changed_fields"] == ["expediente", "requesting_physician_ids"]

    @pytest.mark.parametrize("field,value", [
        ("patient_id", "00000000-0000-0000-0000-000000000001"), ("study_type_id", "x"), ("branch_id", "x"),
        ("order_code", "CU-99"), ("status", "CLOSED"), ("requested_by", "Dr. Otro"), ("billed_lock", False),
        ("tenant_id", "x"), ("anything_else", 1),
    ])
    def test_only_the_allowlist_is_editable(self, client, lab, field, value):
        reg = register(client, lab)
        res = patch(client, lab, reg["order_id"], {field: value})
        assert res.status_code == 422, res.text
        assert res.json()["detail"][0]["type"] == "order_field_not_editable"
        assert detail(client, lab, reg["order_id"])["order_code"] == reg["order_code"]

    def test_empty_body_and_null_list_are_422(self, client, lab):
        reg = register(client, lab)
        assert patch(client, lab, reg["order_id"], {}).status_code == 422
        assert patch(client, lab, reg["order_id"], {"requesting_physician_ids": None}).status_code == 422

    def test_physician_only_order_cannot_lose_its_last_physician(self, client, session, lab, docs):
        res = client.post("/api/v1/laboratory/orders/unified", headers=lab.headers, json={
            **lab.unified(requesting_physician_ids=[str(docs[0].id)]), "patient_id": None})
        order_id = res.json()["order"]["id"]
        assert patch(client, lab, order_id, {"requesting_physician_ids": []}).status_code == 400
        assert patch(client, lab, order_id, {"requesting_physician_ids": [str(docs[1].id)]}).status_code == 200

    def test_derived_requester_text_follows_but_a_typed_one_is_kept(self, client, session, lab, docs):
        derived = register(client, lab, requesting_physician_ids=[str(docs[0].id)], requested_by=None)
        typed = register(client, lab, requesting_physician_ids=[str(docs[0].id)], requested_by="ana@lab.invalid")
        for r in (derived, typed):
            patch(client, lab, r["order_id"], {"requesting_physician_ids": [str(docs[1].id)]})
        assert detail(client, lab, derived["order_id"])["requested_by"] == "Beto Solicitante"
        assert detail(client, lab, typed["order_id"])["requested_by"] == "ana@lab.invalid"

    def test_permission_tenant_and_unknown_order(self, client, session, lab, docs):
        reg = register(client, lab)
        tech = lab.headers_for("lab_tech")  # no lab:update_order
        assert patch(client, lab, reg["order_id"], {"expediente": "1"}, headers=tech).status_code == 403
        other = Lab(session, "Beta")
        assert patch(client, lab, reg["order_id"], {"expediente": "1"}, headers=other.headers).status_code == 404
        assert patch(client, lab, "00000000-0000-0000-0000-000000000009", {"expediente": "1"}).status_code == 404
        assert detail(client, lab, reg["order_id"])["expediente"] is None


# ---------------------------------------------------------------- lifecycle guard


class TestLifecycleGuard:
    @pytest.mark.parametrize("status", [OrderStatus.RECEIVED, OrderStatus.PROCESSING, OrderStatus.DIAGNOSIS])
    def test_window_reception_through_diagnosis_is_open(self, client, session, lab, status):
        reg = register(client, lab)
        set_status(session, reg["order_id"], status)
        assert patch(client, lab, reg["order_id"], {"expediente": "1"}).status_code == 200

    @pytest.mark.parametrize("status", [OrderStatus.REVIEW, OrderStatus.RELEASED, OrderStatus.CLOSED, OrderStatus.CANCELLED])
    def test_review_onward_is_refused(self, client, session, lab, status):
        reg = register(client, lab)
        set_status(session, reg["order_id"], status)
        res = patch(client, lab, reg["order_id"], {"expediente": "1"})
        assert res.status_code == 409
        assert res.json()["detail"]["code"] == "order_not_directly_modifiable"
        assert "order_phase" in {b["code"] for b in res.json()["detail"]["blockers"]}
        assert detail(client, lab, reg["order_id"])["expediente"] is None

    def test_a_draft_report_alone_is_fine(self, client, session, lab):
        reg = register(client, lab)
        make_report(session, lab, reg["order_id"])
        set_status(session, reg["order_id"], OrderStatus.DIAGNOSIS)
        assert patch(client, lab, reg["order_id"], {"expediente": "1"}).status_code == 200

    @pytest.mark.parametrize("evidence", [
        "report_in_review", "approved", "published_at", "submitted_event_after_return", "decided_review",
        "signed_version", "pdf_artifact", "retracted",
    ])
    def test_aggregate_status_is_not_enough(self, client, session, lab, evidence):
        """The order may look like Diagnosis with a DRAFT report and still be protected by history."""
        reg = register(client, lab)
        oid = reg["order_id"]
        status = {"report_in_review": ReportStatus.IN_REVIEW, "approved": ReportStatus.APPROVED,
                  "retracted": ReportStatus.RETRACTED}.get(evidence, ReportStatus.DRAFT)
        report, version = make_report(session, lab, oid, status=status)
        if evidence == "published_at":
            session.execute(text("UPDATE report SET published_at = now() WHERE id = :i"), {"i": str(report.id)})
        elif evidence == "submitted_event_after_return":
            add_event(session, lab, oid, EventType.REPORT_SUBMITTED)  # returned to DRAFT later
        elif evidence == "decided_review":
            decide_review(session, lab, oid, ReviewStatus.REJECTED)
        elif evidence == "signed_version":
            sign_version(session, lab, report.id)
        elif evidence == "pdf_artifact":
            session.execute(text("UPDATE report_version SET pdf_sha256 = :h WHERE report_id = :i"),
                            {"h": "0" * 64, "i": str(report.id)})
        session.commit()
        set_status(session, oid, OrderStatus.DIAGNOSIS)
        res = patch(client, lab, oid, {"expediente": "9", "notes": "n"})
        assert res.status_code == 409, evidence
        assert res.json()["detail"]["blockers"], evidence
        assert detail(client, lab, oid)["expediente"] is None

    def test_pending_reviewer_assignment_is_not_evidence(self, client, session, lab):
        """Reviewers are assigned at registration (default reviewer); only a decision or history counts."""
        reg = register(client, lab)
        from .factories import add_order_reviewer
        from app.models.laboratory import Order
        add_order_reviewer(session, lab.tenant, session.get(Order, reg["order_id"]), lab.user)
        assert patch(client, lab, reg["order_id"], {"expediente": "1"}).status_code == 200

    def test_existing_note_endpoint_follows_the_same_rule(self, client, session, lab):
        reg = register(client, lab)
        url = f"{LAB}/orders/{reg['order_id']}/notes"
        assert client.patch(url, json={"notes": "ok"}, headers=lab.headers).status_code == 200
        set_status(session, reg["order_id"], OrderStatus.REVIEW)
        res = client.patch(url, json={"notes": "bypass"}, headers=lab.headers)
        assert res.status_code == 409
        assert detail(client, lab, reg["order_id"])["notes"] == "ok"
        # history blocks it even when the status looks open
        set_status(session, reg["order_id"], OrderStatus.DIAGNOSIS)
        add_event(session, lab, reg["order_id"], EventType.REPORT_SUBMITTED)
        assert client.patch(url, json={"notes": "bypass"}, headers=lab.headers).status_code == 409

    def test_management_state_explains_the_blockers(self, client, session, lab):
        reg = register(client, lab)
        state = client.get(f"{LAB}/orders/{reg['order_id']}/management", headers=lab.headers).json()
        assert state["edit"] == {"permitted": True, "allowed": True, "blockers": []}
        assert state["editable_fields"] == ["requesting_physician_ids", "expediente", "notes"]
        set_status(session, reg["order_id"], OrderStatus.REVIEW)
        state = client.get(f"{LAB}/orders/{reg['order_id']}/management", headers=lab.headers).json()
        assert state["edit"]["allowed"] is False and state["edit"]["blockers"][0]["code"] == "order_phase"
        tech = client.get(f"{LAB}/orders/{reg['order_id']}/management", headers=lab.headers_for("lab_tech")).json()
        assert tech["edit"]["permitted"] is False and tech["removal"]["permitted"] is False


# ---------------------------------------------------------------- report metadata


class TestReportMetadata:
    def test_authoritative_metadata_lists_all_physicians_and_the_expediente(self, client, session, lab, docs):
        from app.models.laboratory import Order

        reg = register(client, lab, requesting_physician_ids=[str(docs[2].id), str(docs[0].id)], expediente="000731")
        order = session.get(Order, reg["order_id"])
        template = {"base": {
            "requesting_physician": {"is_visible": True, "label": "Médico solicitante", "value": ""},
            "expediente": {"is_visible": True, "label": "Expediente", "value": ""},
        }}
        body = report_metadata.apply_authoritative_report_metadata(
            session, {"base": {"requesting_physician": {"value": "forged"}, "expediente": {"value": "forged"}}}, order, template)
        assert body["base"]["requesting_physician"]["value"] == "Carla Solicitante; Ana Solicitante"
        assert body["base"]["expediente"]["value"] == "000731"
        # a template that does not declare the field never acquires it
        legacy = report_metadata.apply_authoritative_report_metadata(
            session, {"base": {"requesting_physician": {"value": ""}}}, order, {"base": {"requesting_physician": {}}})
        assert "expediente" not in legacy["base"]

    def test_expediente_only_orders_do_not_invent_a_physician(self, client, session, lab):
        from app.models.laboratory import Order

        reg = register(client, lab, expediente="12", requested_by=None)
        order = session.get(Order, reg["order_id"])
        assert report_metadata.resolve_requesting_physician_name(session, order) == ""
        assert report_metadata.resolve_expediente(order) == "12"

    def test_legacy_free_text_is_the_fallback_when_nothing_structured_exists(self, client, session, lab):
        from app.models.laboratory import Order

        reg = register(client, lab, requested_by="Dr. Texto Libre")
        assert report_metadata.resolve_requesting_physician_name(session, session.get(Order, reg["order_id"])) == "Dr. Texto Libre"

    def test_a_frozen_report_is_not_rewritten_by_a_later_edit_attempt(self, client, session, lab, docs):
        """An order with a published report cannot be edited at all, so its issued metadata cannot drift."""
        reg = register(client, lab, requesting_physician_ids=[str(docs[0].id)])
        report, _ = make_report(session, lab, reg["order_id"], status=ReportStatus.PUBLISHED)
        set_status(session, reg["order_id"], OrderStatus.CLOSED)
        assert patch(client, lab, reg["order_id"], {"requesting_physician_ids": [str(docs[1].id)]}).status_code == 409
