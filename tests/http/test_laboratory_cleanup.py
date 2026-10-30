"""Laboratory-content cleanup, end to end over HTTP on real PostgreSQL (Céluma 1.4, #39).

Contract: celuma-engineering `specs/laboratory/laboratory-content-cleanup.md`, ADR 0021.
Synthetic data only. The object store here is the in-memory `FakeS3Service`: it proves WHICH keys the
executor asked to delete, not that bytes physically left a bucket (see the object-store test module).
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from app.models.laboratory_cleanup import LaboratoryCleanupOperation
from app.services import laboratory_cleanup_scope as scope_mod
from app.services import numbering
from app.services.storage_billing import StorageBillingService

from .cleanup_world import SENTINEL, Flow, build_world, object_store, snapshot
from .conftest import FakeS3Service
from .factories import TEST_USER_PASSWORD
from .numbering_support import RECEIVED, UNIFIED, fresh_client, freeze  # noqa: F401

#: tables whose target-tenant rows are the cleanup's DELETE scope (everything else must be identical)
SCOPED = {
    "order", "sample", "sample_image", "sample_image_rendition", "sample_labels", "order_labels", "order_comment",
    "order_comment_mention", "order_event", "order_requesting_physician", "report", "report_version",
    "report_amendment", "report_review", "assignment", "notification", "notification_recipient",
    "notification_delivery", "invoice", "invoice_item", "payment",
}
FINANCE = {"invoice", "invoice_item", "payment"}


@pytest.fixture
def world(session, fresh_client, monkeypatch, stub_pdf_render):
    return build_world(session, fresh_client, monkeypatch, stub_pdf_render)


@pytest.fixture
def flow(fresh_client, world):
    return Flow(fresh_client, world.target)


def scoped_counts(session, tenant_id):
    t = str(tenant_id)
    return {
        st.key: scope_mod.count_rows(session, st, t) for st in scope_mod.tables_for()
    }


def unrelated_audit(session, tenant_id):
    """The tenant's audit rows that are NOT about the removed operational content and not cleanup provenance."""
    return session.execute(text(
        "SELECT coalesce(string_agg(a::text, '|' ORDER BY a.id), '') FROM audit_log a WHERE a.tenant_id = :t "
        "AND a.entity_type NOT IN ('laboratory_cleanup') AND a.action NOT LIKE 'LABORATORY_CLEANUP.%' "
        "AND a.entity_type IN ('sample_type', 'numbering_config', 'study_type')"), {"t": str(tenant_id)}).scalar()


def preserved_view(snap):
    """The part of a tenant snapshot that a cleanup must leave byte-identical."""
    return {t: v for t, v in snap.items() if t not in SCOPED and t not in {
        "audit_log", "tenant_usage", "tenant_usage_threshold_state", "tenant_usage_reconciliation",
        "laboratory_cleanup_operation", "laboratory_cleanup_object", "laboratory_cleanup_retired_code",
        "storage_object"}}


# --------------------------------------------------------------------------- preview


class TestPreview:
    def test_preview_is_read_only_and_describes_the_exact_scope(self, session, flow, world):
        t = world.target.tenant.id
        before_all = {tid: snapshot(session, tid, skip={"audit_log"}) for tid in (t, world.other.tenant.id)}
        audit_before = session.execute(text("SELECT count(*) FROM audit_log")).scalar()
        store_before = object_store()

        r = flow.preview()
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["laboratory"] == {"id": str(t), "name": "Laboratorio Embajador Ñandú"}
        assert "options" not in body  # there is no option: the scope is always the complete one
        d = body["delete"]
        assert d["orders"] == 4 and d["reports"] == 2 and d["report_amendments"] == 1
        assert d["invoices"] == 3 and d["invoice_items"] == 3 and d["payments"] == 1
        assert d["sample_image_renditions"] == 4  # thumb, original_raw, shared_probe, cross_tenant_probe
        assert d["audit_rows"] >= 1   # the historical audit of this content is part of what is deleted
        assert "order_tombstones" not in body["retain_as_evidence"] and "audit_rows_redacted" not in body["retain_as_evidence"]
        assert body["retain_as_evidence"]["previously_removed_orders_included"] == 1
        assert body["retain_as_evidence"]["idempotency_receipts"] == 3
        assert body["preserve"]["patients"] == 1 and body["preserve"]["users"] >= 5
        assert body["preserve"]["numbering_counters"] > 0
        assert body["objects"]["delete_count"] > 0 and body["objects"]["delete_bytes"] > 0
        assert body["objects"]["kept_shared_count"] == 2  # the reviewer signature and the cross-tenant dedup object
        assert body["blockers"] == [] and body["preview_token"] and len(body["scope_revision"]) == 64

        # No domain row changed, no number allocated, no object touched: only one credential-free audit row.
        after_all = {tid: snapshot(session, tid, skip={"audit_log"}) for tid in (t, world.other.tenant.id)}
        assert after_all == before_all
        assert session.execute(text("SELECT count(*) FROM audit_log")).scalar() == audit_before + 1
        assert object_store() == store_before
        row = session.execute(text("SELECT action, new_values, event_metadata::text FROM audit_log "
                                   "WHERE action = 'LABORATORY_CLEANUP.PREVIEWED'")).one()
        assert TEST_USER_PASSWORD not in row[2]

    def test_revision_depends_on_content_not_only_counts(self, session, flow):
        first = flow.preview().json()["scope_revision"]
        assert flow.preview().json()["scope_revision"] == first  # stable when nothing changed
        session.execute(text("UPDATE sample SET notes = 'cambio de contenido' WHERE id = (SELECT id FROM sample LIMIT 1)"))
        session.commit()
        assert flow.preview().json()["scope_revision"] != first

    def test_a_blocker_withholds_the_token(self, session, flow, world):
        session.execute(text("UPDATE notification_delivery SET status = 'SENDING' WHERE tenant_id = :t"),
                        {"t": str(world.target.tenant.id)})
        session.commit()
        body = flow.preview().json()
        assert {b["code"] for b in body["blockers"]} == {"deliveries_in_flight"}
        assert body["preview_token"] is None


# --------------------------------------------------------------------------- execution, both modes


def assert_graph_removed(session, tenant_id):
    for key, value in scoped_counts(session, tenant_id).items():
        assert value == 0, (key, value)
    # ...and so is the historical audit of that content (rows AND payloads), whatever their entity type.
    assert scope_mod.count_rows(session, scope_mod.AUDIT_TABLE, str(tenant_id)) == 0


def assert_referentially_valid(session):
    """No dangling references anywhere in the database (every FK is also enforced by PostgreSQL)."""
    checks = {
        "invoice without order": 'SELECT count(*) FROM invoice i LEFT JOIN "order" o ON o.id = i.order_id WHERE o.id IS NULL',
        "report_version without report": "SELECT count(*) FROM report_version v LEFT JOIN report r ON r.id = v.report_id WHERE r.id IS NULL",
        "rendition without image": "SELECT count(*) FROM sample_image_rendition x LEFT JOIN sample_image i ON i.id = x.sample_image_id WHERE i.id IS NULL",
        "storage reference to missing object": (
            "SELECT count(*) FROM (SELECT storage_id AS s FROM sample_image UNION ALL SELECT storage_id FROM sample_image_rendition "
            "UNION ALL SELECT pdf_storage_id FROM report_version WHERE pdf_storage_id IS NOT NULL "
            "UNION ALL SELECT json_storage_id FROM report_version WHERE json_storage_id IS NOT NULL "
            "UNION ALL SELECT logo_storage_id FROM tenant WHERE logo_storage_id IS NOT NULL "
            "UNION ALL SELECT signature_storage_id FROM app_user WHERE signature_storage_id IS NOT NULL) r "
            "LEFT JOIN storage_object o ON o.id = r.s WHERE o.id IS NULL"),
    }
    for label, sql in checks.items():
        assert session.execute(text(sql)).scalar() == 0, label


class TestCleanupOfTheAmbassadorScenario:
    def test_removes_the_operational_graph_and_preserves_everything_else(self, session, flow, world, caplog):
        t, o = world.target.tenant.id, world.other.tenant.id
        other_before = snapshot(session, o)
        target_before = snapshot(session, t)
        store_before = object_store()
        old_codes = set(world.target.order_codes.values()) | set(world.target.sample_codes)
        audit_unrelated_before = unrelated_audit(session, t)
        usage_before = session.execute(text("SELECT billable_storage_bytes FROM tenant_usage WHERE tenant_id = :t"), {"t": str(t)}).scalar()
        other_usage_before = session.execute(text("SELECT billable_storage_bytes FROM tenant_usage WHERE tenant_id = :t"), {"t": str(o)}).scalar()

        with caplog.at_level("DEBUG"):
            op = flow.run()
        assert op["status"] == "COMPLETED" and op["phase"] == "DONE", (op["status"], op["phase"], op["error_code"], op["error_message"])
        assert op["error_code"] is None and "include_finance" not in op
        assert op["objects"]["pending"] == 0 and op["objects"]["failed"] == 0 and op["objects"]["total"] > 0
        assert op["writers_blocked"] is False and op["can_retry"] is False

        session.expire_all()
        audit_unrelated_after = unrelated_audit(session, t)
        assert_graph_removed(session, t)
        assert_referentially_valid(session)

        # PRESERVE: identical content (hashes), not just surviving counts.
        target_after = snapshot(session, t)
        assert preserved_view(target_after) == preserved_view(target_before)
        for table in ("patient", "requesting_physician", "study_type", "sample_type", "price_catalog", "label",
                      "report_template", "report_template_version", "report_letterhead", "report_letterhead_version",
                      "app_user", "user_role", "branch", "tenant", "numbering_config", "numbering_counter",
                      "numbering_allocation", "idempotency_receipt"):
            assert target_after[table] == target_before[table], table
            assert target_after[table][0] > 0 or table in ("sample_code_correction",), table
        # The other laboratory is untouched in every table it owns.
        assert snapshot(session, o) == other_before

        # Objects: scoped keys are gone, preserved/shared ones are identical, other tenant's untouched.
        store_after = object_store()
        for key in world.target.scoped_keys:
            assert key not in store_after, key
        for key in world.target.preserved_keys | world.target.unprovable_keys | {world.target.shared_key, world.shared_cross}:
            assert store_after[key] == store_before[key], key
        for key in world.other.scoped_keys | world.other.preserved_keys:
            assert store_after[key] == store_before[key], key
        assert set(store_before) - set(store_after) == world.target.scoped_keys

        # Usage is recomputed from what REMAINS: never the stale 999999, never blindly zero.
        actual = StorageBillingService.compute_billable_storage_bytes(session, t)
        stored = session.execute(text("SELECT billable_storage_bytes FROM tenant_usage WHERE tenant_id = :t"), {"t": str(t)}).scalar()
        assert stored == actual and 0 < actual < usage_before
        assert session.execute(text("SELECT billable_storage_bytes FROM tenant_usage WHERE tenant_id = :t"),
                               {"t": str(o)}).scalar() == other_usage_before  # the other tenant's counter is not reconciled

        # Evidence: every removed code is retired; the ledger and counters are untouched.
        retired = {r[0] for r in session.execute(text(
            "SELECT code FROM laboratory_cleanup_retired_code WHERE tenant_id = :t"), {"t": str(t)})}
        assert old_codes <= retired
        sample_code = world.target.sample_codes[0]
        assert numbering.is_occupied(session, t, "sample", sample_code)
        assert numbering.is_occupied(session, t, "sample", f"X-embajador-1")  # a manual/legacy code the ledger never saw
        assert numbering.is_occupied(session, t, "order", world.target.order_codes["O1"])

        # Audit: the history of the removed content is DELETED (rows and payloads, no placeholder); only the
        # cleanup's own technical provenance, and every unrelated audit row, remain.
        dump = session.execute(text("SELECT coalesce(string_agg(a::text, ' '), '') FROM audit_log a WHERE tenant_id = :t"), {"t": str(t)}).scalar()
        assert SENTINEL not in dump and TEST_USER_PASSWORD not in dump and "redacted" not in dump
        assert session.execute(text("SELECT count(*) FROM audit_log WHERE tenant_id = :t AND (order_id IS NOT NULL OR sample_id IS NOT NULL)"),
                               {"t": str(t)}).scalar() == 0
        assert audit_unrelated_after == audit_unrelated_before   # e.g. catalog changes of the laboratory: untouched
        # ...while the other laboratory's identical audit row is retained verbatim.
        assert SENTINEL in session.execute(text("SELECT coalesce(string_agg(a::text, ' '), '') FROM audit_log a WHERE tenant_id = :t"), {"t": str(o)}).scalar()
        actions = [r[0] for r in session.execute(text(
            "SELECT action FROM audit_log WHERE entity_type = 'laboratory_cleanup' ORDER BY created_at"))]
        for expected in ("PREVIEWED", "CONFIRMED", "STARTED", "DATABASE_COMPLETED", "COMPLETED"):
            assert f"LABORATORY_CLEANUP.{expected}" in actions
        assert "SENTINEL" not in " ".join(r.getMessage() for r in caplog.records)
        assert TEST_USER_PASSWORD not in "\n".join(r.getMessage() + repr(r.__dict__) for r in caplog.records)

        # Executed counts match the approved preview.
        approved = op["approved_counts"]["delete"]
        done = op["executed_counts"]["deleted"]
        assert done["audit_rows"] == approved["audit_rows"] >= 1
        for key in ("samples", "sample_images", "reports", "report_versions", "report_amendments", "report_reviews",
                    "invoices", "invoice_items", "payments", "order_comments", "order_events"):
            assert done[key] == approved[key], key
        assert done["orders"] == approved["orders"] == 4

    def test_lists_dashboard_and_idempotent_replays_reflect_the_cleanup(self, session, fresh_client, flow, world):
        lab = world.target.lab
        flow.run()
        orders = fresh_client.get("/api/v1/laboratory/orders/", headers=lab.headers).json()
        assert orders["orders"] == [] and orders["total"] == 0
        samples = fresh_client.get("/api/v1/laboratory/samples/", headers=lab.headers).json()
        assert samples["samples"] == [] and samples["total"] == 0
        for name in ("O1", "O2", "O3", "REMOVED"):
            assert fresh_client.get(f"/api/v1/laboratory/orders/{world.target.orders[name]}", headers=lab.headers).status_code == 404
        assert fresh_client.get("/api/v1/billing/invoices/", headers=lab.headers).json() == []
        # A retry of a cleaned creation is an explicit gone-resource outcome: nothing is recreated or allocated.
        ledger = session.execute(text("SELECT count(*) FROM numbering_allocation")).scalar()
        orders_before = session.execute(text('SELECT count(*) FROM "order"')).scalar()
        replay = fresh_client.post(UNIFIED, json=lab.unified(("BX", "TJ")), headers={**lab.headers, "Idempotency-Key": world.target.keys["O1"]})
        assert replay.status_code == 410 and replay.json()["detail"]["code"] == "idempotent_resource_deleted"
        assert session.execute(text("SELECT count(*) FROM numbering_allocation")).scalar() == ledger
        assert session.execute(text('SELECT count(*) FROM "order"')).scalar() == orders_before

    def test_numbering_never_rewinds_and_a_new_month_follows_the_normal_rules(self, session, fresh_client, flow, world, monkeypatch):
        lab = world.target.lab
        old_codes = set(world.target.order_codes.values()) | set(world.target.sample_codes)
        counters_before = session.execute(text(
            "SELECT entity_kind, scope_id::text, period_month, last_value FROM numbering_counter WHERE tenant_id = :t"),
            {"t": str(lab.tenant.id)}).all()
        flow.run()
        # Same month: continues AFTER the removed codes; nothing issued before is reissued.
        r = fresh_client.post(UNIFIED, json=lab.unified(("BX",)), headers=lab.headers)
        assert r.status_code == 200, r.text
        new_order = r.json()["order"]["order_code"]
        new_samples = [s["sample_code"] for s in r.json()["samples"]]
        assert new_order == "CU041026" and new_order not in old_codes  # O1..O3 were CU01..CU03 in October
        assert not (set(new_samples) & old_codes)
        # A genuinely new monthly period (November, receipt-date numbering of #33) follows the configured rules.
        freeze(monkeypatch, 2026, 11, 2)
        r2 = fresh_client.post(UNIFIED, json=lab.unified(("BX",), received="2026-11-02"), headers=lab.headers)
        assert r2.status_code == 200, r2.text
        assert r2.json()["order"]["order_code"] == "CU011126"
        assert r2.json()["samples"][0]["sample_code"] == "BX011126"
        counters_after = {(k, sc, m): v for k, sc, m, v in session.execute(text(
            "SELECT entity_kind, scope_id::text, period_month, last_value FROM numbering_counter WHERE tenant_id = :t"),
            {"t": str(lab.tenant.id)})}
        assert counters_before
        for kind, scope, month, value in counters_before:
            assert counters_after[(kind, scope, month)] >= value  # never rewound
        assert any(v > before for (k, sc, m, before) in counters_before for v in [counters_after[(k, sc, m)]])  # and it moved forward


class TestBillingIsAlwaysPartOfTheCleanup:
    """Owner decision (2026-10-28, "limpiar todo"): order-linked invoices, items and payments are always removed;
    there is no preservation mode, no read-only retained invoice and no tombstone order. The SaaS plan and the
    price catalog are not order-linked and stay."""

    def test_every_invoice_item_and_payment_goes_and_nothing_financial_of_the_catalog_does(self, session, flow, world):
        t = str(world.target.tenant.id)
        prices = session.execute(text("SELECT coalesce(string_agg(p::text, '|' ORDER BY p.id), '') FROM price_catalog p WHERE tenant_id = :t"), {"t": t}).scalar()
        limits = session.execute(text("SELECT coalesce(string_agg(l::text, '|'), '') FROM tenant_limits l WHERE tenant_id = :t"), {"t": t}).scalar()
        other_billing = session.execute(text("SELECT count(*) FROM invoice WHERE tenant_id <> :t"), {"t": t}).scalar()
        assert flow.run()["status"] == "COMPLETED"
        session.expire_all()
        for table in ("invoice", "invoice_item", "payment", '"order"'):
            assert session.execute(text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"), {"t": t}).scalar() == 0, table
        assert session.execute(text("SELECT count(*) FROM \"order\" WHERE removed_at IS NOT NULL AND tenant_id = :t"), {"t": t}).scalar() == 0  # no tombstones
        assert session.execute(text("SELECT coalesce(string_agg(p::text, '|' ORDER BY p.id), '') FROM price_catalog p WHERE tenant_id = :t"), {"t": t}).scalar() == prices != ""
        assert session.execute(text("SELECT coalesce(string_agg(l::text, '|'), '') FROM tenant_limits l WHERE tenant_id = :t"), {"t": t}).scalar() == limits
        assert session.execute(text("SELECT count(*) FROM invoice WHERE tenant_id <> :t"), {"t": t}).scalar() == other_billing > 0
        assert_referentially_valid(session)

    @pytest.mark.parametrize("value", [True, False, None])
    def test_the_retired_option_is_rejected_whatever_its_value_and_nothing_changes(self, session, fresh_client, flow, world, value):
        token = flow.preview().json()["preview_token"]
        before = {tid: snapshot(session, tid, skip={"audit_log"}) for tid in (world.target.tenant.id, world.other.tenant.id)}
        sent = {"include_finance": value}
        if value is None:   # an explicit JSON null is the same as omitting it: accepted, and the scope is still complete
            assert flow.client.post(f"{flow.base}/preview", json=sent, headers=flow.headers).status_code == 200
            return
        r = flow.client.post(f"{flow.base}/preview", json=sent, headers=flow.headers)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "include_finance_not_supported"
        r = flow.client.post(f"{flow.base}/operations", headers=flow.headers, json={
            "preview_token": token, "include_finance": value, "reason": "Limpieza de contenido de prueba",
            "confirm_name": world.target.tenant.name, "password": TEST_USER_PASSWORD})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "include_finance_not_supported"
        r = flow.client.post(f"{flow.base}/preview", json={"something_else": 1}, headers=flow.headers)
        assert r.status_code == 422   # unknown fields are refused too
        assert {tid: snapshot(session, tid, skip={"audit_log"}) for tid in (world.target.tenant.id, world.other.tenant.id)} == before
        assert session.execute(text("SELECT count(*) FROM laboratory_cleanup_operation")).scalar() == 0

    def test_the_laboratory_keeps_working_afterwards(self, session, fresh_client, flow, world):
        flow.run()
        lab = world.target.lab
        r = fresh_client.post(UNIFIED, json=lab.unified(("BX",)), headers=lab.headers)
        assert r.status_code == 200, r.text
        assert r.json()["order"]["order_code"] == "CU041026"


# --------------------------------------------------------------------------- normal lifecycle unchanged


class TestOrdinaryLifecycleIsUnchanged:
    def test_a_signed_report_still_cannot_be_removed_by_ordinary_capture_removal(self, session, fresh_client, world):
        lab = world.target.lab
        r = fresh_client.post(
            f"/api/v1/laboratory/orders/{world.target.orders['O2']}/remove", headers=lab.headers,
            json={"reason": "Intento de retiro ordinario de contenido firmado"})
        assert r.status_code in (404, 409, 422), r.text
        assert session.execute(text("SELECT count(*) FROM report WHERE tenant_id = :t"), {"t": str(world.target.tenant.id)}).scalar() == 2
        # and the cleanup feature is invisible to ordinary users while disabled for them
        assert fresh_client.get(f"/api/v1/maintenance/tenants/{world.target.tenant.id}/laboratory-cleanup/authorization",
                                headers=world.target.lab.headers_for("lab_tech")).json() == {"authorized": False}


class TestLegacyNumberingStaysAboveRemovedCodes:
    def test_the_compatibility_allocator_never_reissues_a_removed_manual_code(self, session, fresh_client, monkeypatch):
        """A laboratory that never activated monthly numbering: its codes are `<code>-<n>` and a manual/legacy code
        such as `CU-50` was never in the ledger. Without the retired-code table the high-water mark would fall back
        to 3 after the cleanup and `CU-50` would be issued again by `CU-51`'s predecessors."""
        from app.models.laboratory import Order
        from .cleanup_world import LabWorld
        from .numbering_support import Lab

        freeze(monkeypatch, 2026, 10, 15)
        lab = Lab(session, "Legado", sample_types=("BX",))  # no numbering configuration at all
        for _ in range(2):
            assert fresh_client.post(UNIFIED, json=lab.unified(("BX",)), headers=lab.headers).status_code == 200
        session.add(Order(tenant_id=lab.tenant.id, branch_id=lab.branch.id, order_code="CU-50"))  # manual/legacy, no ledger row
        session.commit()
        codes_before = sorted(r[0] for r in session.execute(text('SELECT order_code FROM "order"')))
        assert codes_before == ["CU-1", "CU-2", "CU-50"]
        lab.tenant.created_by_user_id = lab.user.id   # what registration records
        session.add(lab.tenant)
        session.commit()
        world = LabWorld(lab=lab)
        assert Flow(fresh_client, world).run()["status"] == "COMPLETED"
        assert session.execute(text('SELECT count(*) FROM "order"')).scalar() == 0
        created = fresh_client.post(UNIFIED, json=lab.unified(("BX",)), headers=lab.headers)
        assert created.status_code == 200, created.text
        assert created.json()["order"]["order_code"] == "CU-51"  # above the removed manual CU-50
        for code in ("CU-1", "CU-2", "CU-50"):
            assert numbering.is_occupied(session, lab.tenant.id, "order", code)
