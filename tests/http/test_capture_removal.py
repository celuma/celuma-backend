"""Auditable removal of erroneous orders and samples (#11), over real HTTP + PostgreSQL.

Removal is a tombstone: the row, its code, ledger entry, invoice, reports and audit trail stay; every
ordinary query stops seeing it. Contract: `celuma-engineering/specs/laboratory/order-sample-management.md`.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.models.billing import Payment
from app.models.enums import EventType, OrderStatus, PaymentStatus, ReportStatus

from .management_support import LAB, add_event, count, decide_review, make_report, register, set_status
from .numbering_support import (
    ORDERS, SAMPLES, UNIFIED, Lab, configure, counter_rows, freeze, fresh_client, ledger_rows, run_parallel,  # noqa: F401
)

API = "/api/v1"


@pytest.fixture
def lab(session, monkeypatch):
    freeze(monkeypatch)
    return Lab(session, "Alfa", sample_types=("BX", "TJ"))


def remove_sample(client, lab, sample_id, reason="captura duplicada", headers=None):
    return client.post(f"{LAB}/samples/{sample_id}/removal", json={"reason": reason}, headers=headers or lab.headers)


def remove_order(client, lab, order_id, reason="orden capturada por error", headers=None):
    return client.post(f"{LAB}/orders/{order_id}/removal", json={"reason": reason}, headers=headers or lab.headers)


def visible_order_ids(client, lab):
    return [o["id"] for o in client.get(f"{LAB}/orders/", headers=lab.headers).json()["orders"]]


def visible_sample_ids(client, lab):
    return [s["id"] for s in client.get(f"{LAB}/samples/", headers=lab.headers).json()["samples"]]


class TestSampleRemoval:
    def test_removes_one_sample_keeping_the_order_and_the_others(self, client, session, lab):
        reg = register(client, lab, ("BX", "TJ"))
        gone, kept = reg["sample_ids"]
        res = remove_sample(client, lab, gone)
        assert res.status_code == 200, res.text
        body = res.json()
        assert (body["kind"], body["already_removed"], body["code"]) == ("sample", False, reg["sample_codes"][0])
        assert visible_sample_ids(client, lab) == [kept]
        assert client.get(f"{LAB}/samples/{gone}", headers=lab.headers).status_code == 404
        full = client.get(f"{LAB}/orders/{reg['order_id']}/full", headers=lab.headers).json()
        assert [s["id"] for s in full["samples"]] == [kept]
        opts = client.get(f"{LAB}/samples/filter-options", headers=lab.headers).json()
        assert [o["value"] for o in opts["orders"]] == [reg["order_id"]]
        # the row and its identifiers are retained (a tombstone, not a delete)
        row = session.execute(text("SELECT sample_code, removed_at, removal_reason FROM sample WHERE id = :i"), {"i": gone}).one()
        assert row.sample_code == reg["sample_codes"][0] and row.removed_at is not None and row.removal_reason == "captura duplicada"

    def test_direct_mutations_of_a_removed_sample_are_404(self, client, session, lab):
        reg = register(client, lab, ("BX", "BX"))
        sid = reg["sample_ids"][0]
        remove_sample(client, lab, sid)
        for method, url, body in (
            ("patch", f"{LAB}/samples/{sid}/state", {"state": "PROCESSING"}),
            ("patch", f"{LAB}/samples/{sid}/notes", {"notes": "x"}),
            ("put", f"{LAB}/samples/{sid}/labels", {"label_ids": []}),
            ("put", f"{LAB}/samples/{sid}/assignees", {"assignee_ids": []}),
            ("post", f"{LAB}/samples/{sid}/code-correction", {"sample_code": "OK1", "reason": "abc"}),
        ):
            assert getattr(client, method)(url, json=body, headers=lab.headers).status_code == 404, url
        assert client.get(f"{LAB}/samples/{sid}/images", headers=lab.headers).status_code == 404

    def test_the_code_is_never_reissued_and_no_other_sample_is_renumbered(self, client, session, lab):
        reg = register(client, lab, ("BX", "BX", "BX"))
        assert reg["sample_codes"] == ["BX-1", "BX-2", "BX-3"]
        remove_sample(client, lab, reg["sample_ids"][2])  # the highest one
        again = register(client, lab, ("BX",))
        assert again["sample_codes"] == ["BX-4"]
        assert ledger_rows(session) >= 4
        assert {c for c in session.execute(text("SELECT sample_code FROM sample")).scalars()} == {"BX-1", "BX-2", "BX-3", "BX-4"}

    def test_the_orders_last_sample_is_not_removable_and_the_order_is_never_removed_for_it(self, client, session, lab):
        reg = register(client, lab, ("BX",))
        res = remove_sample(client, lab, reg["sample_ids"][0])
        assert res.status_code == 409
        assert "last_sample" in {b["code"] for b in res.json()["detail"]["blockers"]}
        assert reg["order_id"] in visible_order_ids(client, lab) and visible_sample_ids(client, lab) == reg["sample_ids"]
        state = client.get(f"{LAB}/samples/{reg['sample_ids'][0]}/management", headers=lab.headers).json()
        assert state["removal"]["allowed"] is False
        # registering the replacement first makes the wrong one removable
        client.post(SAMPLES, json=lab.sample_payload(reg["order_id"]), headers=lab.headers)
        assert remove_sample(client, lab, reg["sample_ids"][0]).status_code == 200

    def test_status_is_recalculated_from_the_remaining_samples(self, client, session, lab):
        reg = register(client, lab, ("BX", "BX"))
        client.patch(f"{LAB}/samples/{reg['sample_ids'][0]}/state", json={"state": "PROCESSING"}, headers=lab.headers)
        assert client.get(f"{LAB}/orders/{reg['order_id']}", headers=lab.headers).json()["status"] == "PROCESSING"
        remove_sample(client, lab, reg["sample_ids"][0])
        assert client.get(f"{LAB}/orders/{reg['order_id']}", headers=lab.headers).json()["status"] == "RECEIVED"

    @pytest.mark.parametrize("status", [OrderStatus.REVIEW, OrderStatus.RELEASED, OrderStatus.CLOSED, OrderStatus.CANCELLED])
    def test_refused_from_review_onward(self, client, session, lab, status):
        reg = register(client, lab, ("BX", "BX"))
        set_status(session, reg["order_id"], status)
        assert remove_sample(client, lab, reg["sample_ids"][0]).status_code == 409
        assert len(visible_sample_ids(client, lab)) == 2

    def test_report_history_protects_linked_samples_too(self, client, session, lab):
        reg = register(client, lab, ("BX", "BX"))
        make_report(session, lab, reg["order_id"])
        set_status(session, reg["order_id"], OrderStatus.DIAGNOSIS)
        add_event(session, lab, reg["order_id"], EventType.REPORT_CHANGES_REQUESTED)  # returned to draft
        res = remove_sample(client, lab, reg["sample_ids"][0])
        assert res.status_code == 409
        assert "report_review_history" in {b["code"] for b in res.json()["detail"]["blockers"]}

    def test_retry_after_a_lost_response_is_safe(self, client, session, lab):
        reg = register(client, lab, ("BX", "BX"))
        first = remove_sample(client, lab, reg["sample_ids"][0])
        audits = count(session, "SELECT count(*) FROM audit_log WHERE action = 'SAMPLE.REMOVED'")
        second = remove_sample(client, lab, reg["sample_ids"][0], reason="otro motivo")
        assert second.status_code == 200 and second.json()["already_removed"] is True
        assert second.json()["reason"] == "captura duplicada" and second.json()["removed_at"] == first.json()["removed_at"]
        assert count(session, "SELECT count(*) FROM audit_log WHERE action = 'SAMPLE.REMOVED'") == audits

    def test_permission_reason_and_tenant(self, client, session, lab):
        reg = register(client, lab, ("BX", "BX"))
        sid = reg["sample_ids"][0]
        assert remove_sample(client, lab, sid, headers=lab.headers_for("lab_tech")).status_code == 403
        assert remove_sample(client, lab, sid, headers=lab.headers_for("pathologist")).status_code == 403
        assert remove_sample(client, lab, sid, reason=" ").status_code == 422
        assert remove_sample(client, lab, sid, headers=Lab(session, "Beta").headers).status_code == 404
        assert len(visible_sample_ids(client, lab)) == 2
        assert remove_sample(client, lab, sid, headers=lab.headers_for("admin")).status_code == 200

    def test_audit_trail_names_actor_reason_and_codes(self, client, session, lab):
        reg = register(client, lab, ("BX", "BX"))
        remove_sample(client, lab, reg["sample_ids"][1], reason="muestra duplicada")
        row = session.execute(text("SELECT actor_user_id, event_metadata FROM audit_log WHERE action = 'SAMPLE.REMOVED'")).one()
        assert str(row.actor_user_id) == str(lab.user.id)
        assert row.event_metadata["reason"] == "muestra duplicada" and row.event_metadata["sample_code"] == reg["sample_codes"][1]
        event = session.execute(text("SELECT event_metadata FROM order_event WHERE event_type = 'SAMPLE_REMOVED'")).scalar_one()
        assert event["sample_code"] == reg["sample_codes"][1]


class TestOrderRemoval:
    def test_order_samples_and_draft_report_disappear_from_every_view(self, client, session, lab):
        keep = register(client, lab, ("BX",))
        reg = register(client, lab, ("BX", "TJ"))
        report, _ = make_report(session, lab, reg["order_id"])
        session.execute(text("UPDATE report SET title = 'Informe de prueba' WHERE id = :i"), {"i": str(report.id)})
        session.commit()
        set_status(session, reg["order_id"], OrderStatus.DIAGNOSIS)
        before = client.get(f"{API}/dashboard/", headers=lab.headers).json()["stats"]
        res = remove_order(client, lab, reg["order_id"])
        assert res.status_code == 200, res.text
        body = res.json()
        assert (body["kind"], body["samples_removed"], body["reports_hidden"], body["invoice_retained"]) == ("order", 2, 1, True)
        gone = reg["order_id"]
        assert visible_order_ids(client, lab) == [keep["order_id"]]
        assert visible_sample_ids(client, lab) == keep["sample_ids"]
        for url in (f"{LAB}/orders/{gone}", f"{LAB}/orders/{gone}/full", f"{LAB}/orders/{gone}/comments",
                    f"{LAB}/orders/{gone}/events", f"{LAB}/samples/{reg['sample_ids'][0]}",
                    f"{API}/reports/{report.id}", f"{API}/billing/orders/{gone}/invoice"):
            assert client.get(url, headers=lab.headers).status_code == 404, url
        assert client.get(f"{API}/reports/", headers=lab.headers).json()["reports"] == []
        opts = client.get(f"{LAB}/orders/filter-options", headers=lab.headers).json()
        assert gone not in {o.get("value") for o in opts.get("patients", [])}
        dash = client.get(f"{API}/dashboard/", headers=lab.headers).json()
        after = dash["stats"]
        assert gone not in {a["id"] for a in dash["recent_activity"]}
        assert str(report.id) not in {a["id"] for a in dash["recent_activity"]}
        assert after["total_orders"] == before["total_orders"] - 1
        assert after["total_samples"] == before["total_samples"] - 2
        assert after["total_reports"] == before["total_reports"] - 1
        for method, url, body in (
            ("patch", f"{LAB}/orders/{gone}", {"expediente": "1"}),
            ("patch", f"{LAB}/orders/{gone}/notes", {"notes": "x"}),
            ("put", f"{LAB}/orders/{gone}/labels", {"label_ids": []}),
            ("put", f"{LAB}/orders/{gone}/assignees", {"assignee_ids": []}),
            ("post", f"{LAB}/orders/{gone}/comments", {"text": "hola"}),
            ("post", f"{API}/reports/", {"tenant_id": str(lab.tenant.id), "branch_id": str(lab.branch.id), "order_id": gone}),
        ):
            assert getattr(client, method)(url, json=body, headers=lab.headers).status_code == 404, url

    def test_everything_is_retained_in_the_database(self, client, session, lab):
        reg = register(client, lab, ("BX",))
        report, _ = make_report(session, lab, reg["order_id"])
        remove_order(client, lab, reg["order_id"])
        oid = reg["order_id"]
        assert count(session, 'SELECT count(*) FROM "order" WHERE id = :i AND removed_at IS NOT NULL AND removal_reason IS NOT NULL', i=oid) == 1
        assert count(session, "SELECT count(*) FROM sample WHERE order_id = :i AND removed_at IS NOT NULL", i=oid) == 1
        assert count(session, "SELECT count(*) FROM report WHERE id = :i AND removed_at IS NOT NULL", i=str(report.id)) == 1
        assert count(session, "SELECT count(*) FROM invoice WHERE order_id = :i", i=oid) == 1
        assert count(session, "SELECT count(*) FROM numbering_allocation WHERE entity_id = :i", i=oid) == 1  # the order's own ledger entry

    def test_the_invoice_is_hidden_from_billing_but_never_voided_or_deleted(self, client, session, lab):
        reg = register(client, lab)
        before = client.get(f"{API}/billing/invoices/", headers=lab.headers).json()
        assert len(before) == 1
        remove_order(client, lab, reg["order_id"])
        assert client.get(f"{API}/billing/invoices/", headers=lab.headers).json() == []
        row = session.execute(text("SELECT status FROM invoice WHERE order_id = :i"), {"i": reg["order_id"]}).one()
        assert row.status == "PENDING"

    def test_payments_or_a_settled_invoice_block_the_removal(self, client, session, lab):
        reg = register(client, lab)
        invoice = session.execute(text("SELECT id FROM invoice WHERE order_id = :i"), {"i": reg["order_id"]}).scalar_one()
        session.add(Payment(tenant_id=lab.tenant.id, invoice_id=invoice, amount=10))
        session.commit()
        res = remove_order(client, lab, reg["order_id"])
        assert res.status_code == 409 and "invoice_has_payments" in {b["code"] for b in res.json()["detail"]["blockers"]}
        session.execute(text("DELETE FROM payment"))
        session.execute(text("UPDATE invoice SET status = 'PAID' WHERE id = :i"), {"i": invoice})
        session.commit()
        res = remove_order(client, lab, reg["order_id"])
        assert res.status_code == 409 and "invoice_not_pending" in {b["code"] for b in res.json()["detail"]["blockers"]}
        assert reg["order_id"] in visible_order_ids(client, lab)  # nothing silently refunded, voided or deleted

    @pytest.mark.parametrize("status", [OrderStatus.REVIEW, OrderStatus.RELEASED, OrderStatus.CLOSED, OrderStatus.CANCELLED])
    def test_refused_from_review_onward(self, client, session, lab, status):
        reg = register(client, lab)
        set_status(session, reg["order_id"], status)
        res = remove_order(client, lab, reg["order_id"])
        assert res.status_code == 409 and res.json()["detail"]["code"] == "order_not_directly_modifiable"
        assert reg["order_id"] in visible_order_ids(client, lab)

    @pytest.mark.parametrize("evidence", ["in_review", "submitted_history", "decided_review", "published"])
    def test_a_draft_returned_from_review_is_not_removable(self, client, session, lab, evidence):
        reg = register(client, lab)
        status = {"in_review": ReportStatus.IN_REVIEW, "published": ReportStatus.PUBLISHED}.get(evidence, ReportStatus.DRAFT)
        make_report(session, lab, reg["order_id"], status=status)
        if evidence == "submitted_history":
            add_event(session, lab, reg["order_id"], EventType.REPORT_SUBMITTED)
        if evidence == "decided_review":
            decide_review(session, lab, reg["order_id"])
        set_status(session, reg["order_id"], OrderStatus.DIAGNOSIS)
        assert remove_order(client, lab, reg["order_id"]).status_code == 409
        assert reg["order_id"] in visible_order_ids(client, lab)

    def test_codes_are_never_reused_and_nothing_is_renumbered(self, client, session, lab):
        configure(session, lab)
        a, b, c = (register(client, lab, ("BX",)) for _ in range(3))
        assert [a["order_code"], b["order_code"], c["order_code"]] == ["CU011026", "CU021026", "CU031026"]
        counters, ledger = counter_rows(session), ledger_rows(session)
        remove_order(client, lab, c["order_id"])  # the newest
        d = register(client, lab, ("BX",))
        assert d["order_code"] == "CU041026"  # CU031026 is NOT reissued
        assert d["sample_codes"] == ["BX041026"]
        assert counter_rows(session) == counters and ledger_rows(session) == ledger + 2
        assert count(session, "SELECT count(*) FROM numbering_allocation WHERE code = 'CU031026'") == 1
        assert {a["order_code"], b["order_code"]} <= set(session.execute(text('SELECT order_code FROM "order"')).scalars())

    def test_replaying_the_creation_of_a_removed_order_is_refused_not_recreated(self, client, session, lab):
        headers = {**lab.headers, "Idempotency-Key": "replay-after-removal-1"}
        first = client.post(UNIFIED, json=lab.unified(("BX",)), headers=headers)
        assert first.status_code == 200
        oid = first.json()["order"]["id"]
        assert remove_order(client, lab, oid).status_code == 200
        orders = count(session, 'SELECT count(*) FROM "order"')
        replay = client.post(UNIFIED, json=lab.unified(("BX",)), headers=headers)
        assert replay.status_code == 410 and replay.json()["detail"]["code"] == "idempotent_resource_deleted"
        assert count(session, 'SELECT count(*) FROM "order"') == orders
        assert visible_order_ids(client, lab) == []

    def test_retry_after_a_lost_response_is_safe(self, client, session, lab):
        reg = register(client, lab)
        first = remove_order(client, lab, reg["order_id"])
        second = remove_order(client, lab, reg["order_id"], reason="distinto")
        assert second.status_code == 200 and second.json()["already_removed"] is True
        assert second.json()["reason"] == "orden capturada por error" and second.json()["removed_at"] == first.json()["removed_at"]
        assert count(session, "SELECT count(*) FROM audit_log WHERE action = 'ORDER.REMOVED'") == 1

    def test_permission_reason_tenant_and_audit(self, client, session, lab):
        reg = register(client, lab, ("BX",))
        assert remove_order(client, lab, reg["order_id"], headers=lab.headers_for("lab_tech")).status_code == 403
        assert remove_order(client, lab, reg["order_id"], reason="x").status_code == 422
        assert remove_order(client, lab, reg["order_id"], headers=Lab(session, "Beta").headers).status_code == 404
        assert remove_order(client, lab, "00000000-0000-0000-0000-000000000009").status_code == 404
        assert reg["order_id"] in visible_order_ids(client, lab)
        assert remove_order(client, lab, reg["order_id"], reason="captura en el paciente equivocado").status_code == 200
        row = session.execute(text("SELECT actor_user_id, event_metadata FROM audit_log WHERE action = 'ORDER.REMOVED'")).one()
        assert str(row.actor_user_id) == str(lab.user.id)
        assert row.event_metadata["reason"] == "captura en el paciente equivocado"
        assert row.event_metadata["order_code"] == reg["order_code"] and row.event_metadata["sample_codes"] == reg["sample_codes"]

    def test_assignments_and_worklists_do_not_break_or_leak(self, client, session, lab):
        from .factories import assign_to_order
        from app.models.laboratory import Order
        reg = register(client, lab)
        assign_to_order(session, lab.tenant, session.get(Order, reg["order_id"]), lab.user)
        before = client.get(f"{API}/me/worklist", headers=lab.headers)
        assert before.status_code == 200
        remove_order(client, lab, reg["order_id"])
        after = client.get(f"{API}/me/worklist", headers=lab.headers)
        assert after.status_code == 200
        assert all(i["item_id"] != reg["order_id"] for i in after.json().get("items", []))
        assert client.get(f"{API}/assignments", headers=lab.headers).status_code == 200

    def test_the_physician_portal_does_not_list_a_removed_order(self, client, session, lab):
        from .factories import auth_headers, create_user
        reg = register(client, lab, requested_by="medico@alfa.invalid")
        doc = create_user(session, lab.tenant, email="medico@alfa.invalid", roles=("superuser",))
        listed = client.get(f"{API}/portal/physician/orders", headers=auth_headers(doc)).json()
        assert [o["id"] for o in listed] == [reg["order_id"]]
        remove_order(client, lab, reg["order_id"])
        assert client.get(f"{API}/portal/physician/orders", headers=auth_headers(doc)).json() == []

    def test_management_state_tells_the_consequences(self, client, session, lab):
        reg = register(client, lab, ("BX", "TJ"))
        state = client.get(f"{LAB}/orders/{reg['order_id']}/management", headers=lab.headers).json()
        assert state["removal"] == {"permitted": True, "allowed": True, "blockers": []}
        assert state["sample_count"] == 2
        assert any("never reused" in line for line in state["removal_consequences"])
        assert any("2 sample" in line for line in state["removal_consequences"])
        set_status(session, reg["order_id"], OrderStatus.REVIEW)
        state = client.get(f"{LAB}/orders/{reg['order_id']}/management", headers=lab.headers).json()
        assert state["removal"]["allowed"] is False


class TestRemovalRaces:
    def test_removal_racing_sample_creation_never_leaves_a_visible_sample_under_a_removed_order(self, fresh_client, session, lab):
        for _ in range(3):
            reg = register(fresh_client, lab, ("BX",))
            jobs = [lambda c: remove_order(c, lab, reg["order_id"])]
            jobs += [lambda c: c.post(SAMPLES, json=lab.sample_payload(reg["order_id"]), headers=lab.headers) for _ in range(3)]
            results = run_parallel(jobs)
            assert not any(isinstance(r, Exception) for r in results), results
            assert results[0].status_code == 200
            assert all(r.status_code in (200, 404) for r in results[1:]), [r.text for r in results[1:]]
            assert count(session, "SELECT count(*) FROM sample WHERE order_id = :i AND removed_at IS NULL", i=reg["order_id"]) == 0

    def test_two_removals_of_one_order_have_one_effect(self, fresh_client, session, lab):
        reg = register(fresh_client, lab, ("BX",))
        results = run_parallel([lambda c: remove_order(c, lab, reg["order_id"]) for _ in range(4)])
        assert all(not isinstance(r, Exception) and r.status_code == 200 for r in results), results
        assert sorted(r.json()["already_removed"] for r in results) == [False, True, True, True]
        assert count(session, "SELECT count(*) FROM audit_log WHERE action = 'ORDER.REMOVED'") == 1

    def test_removal_racing_report_creation_is_serialized(self, fresh_client, session, lab):
        from app.models.report import ReportTemplate
        from app.services.report_template_hash import hash_clinical_template_block
        from .factories import create_default_letterhead, valid_rendering_snapshot

        # Céluma 1.4 (#22): only a V2 request creates a report. A selector-less body is refused (409
        # `reports_v2_template_required`) whichever side wins, which would leave nothing racing the removal.
        template = ReportTemplate(tenant_id=lab.tenant.id, name="Plantilla", template_json=valid_rendering_snapshot()["template"])
        session.add(template)
        session.commit()
        create_default_letterhead(session, lab.tenant)
        reg = register(fresh_client, lab, ("BX",))
        body = {
            "tenant_id": str(lab.tenant.id), "branch_id": str(lab.branch.id), "order_id": reg["order_id"],
            "template_id": str(template.id), "template_hash": hash_clinical_template_block(template.template_json),
            "report": {"base": {}, "sections": {}},
        }
        results = run_parallel([
            lambda c: remove_order(c, lab, reg["order_id"]),
            lambda c: c.post(f"{API}/reports/", json=body, headers=lab.headers),
        ])
        assert not any(isinstance(r, Exception) for r in results), results
        assert results[0].status_code in (200, 409)
        assert results[1].status_code in (200, 404), results[1].text
        # either the report was created first (a DRAFT, removable: hidden with the order) or the order was
        # removed first (404); never a visible report of a removed order
        removed = count(session, 'SELECT count(*) FROM "order" WHERE id = :i AND removed_at IS NOT NULL', i=reg["order_id"])
        visible = count(session, "SELECT count(*) FROM report WHERE order_id = :i AND removed_at IS NULL", i=reg["order_id"])
        assert not (removed and visible)
