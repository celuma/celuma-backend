"""Removal of study types and sample types from the catalog (Céluma 1.5, issue #94).

Contract: `celuma-engineering/specs/laboratory/catalog-type-removal.md` (proposed ADR 0025). Removal is a
retained tombstone, distinct from deactivation: same id and code, every reference kept, hidden from the
catalog and refused by every creation, selection, transfer, pricing and numbering write. Code reuse (owner
decision 2026-11-25): an unused removed type releases its code for a DIFFERENT type; any order (study type)
or sample (sample type), withdrawn ones included, keeps it reserved. Real PostgreSQL over HTTP; synthetic
data only.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.models.price_catalog import PriceCatalog
from app.models.sample_type import SampleType
from app.models.study_type import StudyType

from .management_support import LAB, register
from .numbering_support import (  # noqa: F401
    NUMBERING, ORDERS, SAMPLES, UNIFIED, Lab, configure, delete_order_and_samples, freeze, fresh_client,
    run_parallel,
)

STUDY = "/api/v1/study-types/"
SAMPLE = "/api/v1/sample-types/"
PRICES = "/api/v1/price-catalog/"
KEY = "Idempotency-Key"
BASES = {"study": STUDY, "sample": SAMPLE}


@pytest.fixture
def lab(session, monkeypatch):
    freeze(monkeypatch)  # 2026-10-15 18:00 UTC
    lab = Lab(session, "Alfa", sample_types=("BX", "TJ"))
    configure(session, lab)
    return lab


@pytest.fixture
def other(session):
    other = Lab(session, "Beta", study="CU", sample_types=("BX",))
    configure(session, other)
    return other


def remove(client, lab, kind, type_id, headers=None, **body):
    return client.post(f"{BASES[kind]}{type_id}/remove", json=body or None, headers=headers or lab.headers)


def type_row(lab, kind):
    return lab.study_type if kind == "study" else lab.sample_types["BX"]


def audit(session: Session, action: str, entity_id=None) -> list:
    sql = "SELECT entity_id, old_values, new_values, event_metadata, tenant_id, actor_user_id FROM audit_log WHERE action = :a"
    params = {"a": action}
    if entity_id is not None:
        sql += " AND entity_id = :e"
        params["e"] = str(entity_id)
    return session.execute(text(sql), params).all()


def listed(client, lab, kind, **params) -> list:
    key = "study_types" if kind == "study" else "sample_types"
    r = client.get(BASES[kind], params=params, headers=lab.headers)
    assert r.status_code == 200, r.text
    return [row["code"] for row in r.json()[key]]


def count(session, sql, **params) -> int:
    return int(session.execute(text(sql), params).scalar_one())


# ---------------------------------------------------------------------------
# Removal itself: unreferenced and referenced, both catalogs
# ---------------------------------------------------------------------------


class TestRemoval:
    @pytest.mark.parametrize("kind", ["study", "sample"])
    def test_unreferenced_type_is_retained_hidden_and_audited(self, client, session, lab, kind):
        row = type_row(lab, kind)
        r = remove(client, lab, kind, row.id, reason="Duplicado por error")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == str(row.id) and body["code"] == row.code
        assert body["is_removed"] is True and body["is_active"] is False and body["removed_at"]
        assert body["already_removed"] is False
        assert all(v == 0 for v in body["references"].values())
        assert body["code_released"] is True  # unused: the code is free for another type

        # Retained: the same row, id and code; never erased even though nothing references it.
        table = "study_type" if kind == "study" else "sample_type"
        stored = session.execute(text(f"SELECT code, is_active, removed_at, removed_by, removal_reason FROM {table} "
                                      "WHERE id = :i"), {"i": str(row.id)}).one()
        assert stored.code == row.code and stored.is_active is False and stored.removed_at is not None
        assert str(stored.removed_by) == str(lab.user.id) and stored.removal_reason == "Duplicado por error"

        # Hidden from the ordinary catalog (active and administration views), visible on request and by id.
        assert row.code not in listed(client, lab, kind)
        assert row.code not in listed(client, lab, kind, active_only="false")
        assert row.code in listed(client, lab, kind, active_only="false", include_removed="true")
        detail = client.get(f"{BASES[kind]}{row.id}", headers=lab.headers)
        assert detail.status_code == 200 and detail.json()["is_removed"] is True

        action = "STUDY_TYPE.REMOVED" if kind == "study" else "SAMPLE_TYPE.REMOVED"
        rows = audit(session, action, row.id)
        assert len(rows) == 1
        entity_id, old, new, meta, tenant_id, actor = rows[0]
        assert old["is_active"] is True and "removed_at" not in old
        assert new["is_active"] is False and new["removal_reason"] == "Duplicado por error"
        assert meta["was_active"] is True and all(v == 0 for v in meta["references"].values())
        assert meta["code_released"] is True and new["code_released"] is True
        assert str(tenant_id) == str(lab.tenant.id) and str(actor) == str(lab.user.id)

    @pytest.mark.parametrize("kind", ["study", "sample"])
    def test_repeating_a_removal_is_answered_without_a_second_write(self, client, session, lab, kind):
        row = type_row(lab, kind)
        first = remove(client, lab, kind, row.id).json()
        again = remove(client, lab, kind, row.id, reason="otra razón")
        assert again.status_code == 200 and again.json()["already_removed"] is True
        assert again.json()["removed_at"] == first["removed_at"]
        action = "STUDY_TYPE.REMOVED" if kind == "study" else "SAMPLE_TYPE.REMOVED"
        assert len(audit(session, action, row.id)) == 1
        table = "study_type" if kind == "study" else "sample_type"
        assert session.execute(text(f"SELECT removal_reason FROM {table} WHERE id = :i"),
                               {"i": str(row.id)}).scalar_one() is None

    @pytest.mark.parametrize("kind", ["study", "sample"])
    def test_concurrent_removals_write_exactly_one_audit_row(self, fresh_client, session, lab, kind):
        row = type_row(lab, kind)
        url = f"{BASES[kind]}{row.id}/remove"
        results = run_parallel([lambda c: c.post(url, headers=lab.headers) for _ in range(2)])
        assert sorted(r.status_code for r in results) == [200, 200]
        assert sorted(r.json()["already_removed"] for r in results) == [False, True]
        action = "STUDY_TYPE.REMOVED" if kind == "study" else "SAMPLE_TYPE.REMOVED"
        assert len(audit(session, action, row.id)) == 1

    def test_referenced_sample_type_keeps_every_historical_read(self, client, session, lab):
        reg = register(client, lab, ("BX", "TJ"))
        bx = lab.sample_types["BX"]
        r = remove(client, lab, "sample", bx.id)
        assert r.status_code == 200 and r.json()["references"]["samples"] == 1
        assert r.json()["code_released"] is False
        assert audit(session, "SAMPLE_TYPE.REMOVED", bx.id)[0][3]["code_released"] is False

        # The sample keeps its reference, code and type text; it is shown as removed.
        sample = client.get(f"{LAB}/samples/{reg['sample_ids'][0]}", headers=lab.headers).json()
        assert sample["sample_code"] == reg["sample_codes"][0]
        assert sample["sample_type"] == {"id": str(bx.id), "code": "BX", "name": bx.name,
                                         "is_active": False, "is_removed": True}
        full = client.get(f"{LAB}/orders/{reg['order_id']}/full", headers=lab.headers).json()
        refs = {s["sample_type"]["code"]: s["sample_type"]["is_removed"] for s in full["samples"]}
        assert refs == {"BX": True, "TJ": False}

        # Lists and filters still find it, marked as removed.
        page = client.get(f"{LAB}/samples/", params={"sample_type_id": str(bx.id)}, headers=lab.headers).json()
        assert [i["id"] for i in page["samples"]] == [reg["sample_ids"][0]]
        options = client.get(f"{LAB}/samples/filter-options", headers=lab.headers).json()["sample_types"]
        assert {"value": str(bx.id), "text": f"{bx.name} (eliminado)"} in options
        found = client.get(f"{LAB}/samples/", params={"q": "BX"}, headers=lab.headers).json()["samples"]
        assert reg["sample_ids"][0] in [i["id"] for i in found]
        assert count(session, "SELECT count(*) FROM sample WHERE sample_type_id = :i", i=str(bx.id)) == 1

    def test_referenced_study_type_keeps_orders_prices_and_report_defaults(self, client, session, lab):
        reg = register(client, lab, ("BX",))
        price = PriceCatalog(tenant_id=lab.tenant.id, study_type_id=lab.study_type.id, unit_price=100)
        session.add(price)
        session.commit()
        r = remove(client, lab, "study", lab.study_type.id)
        assert r.status_code == 200
        assert r.json()["references"]["orders"] == 1 and r.json()["references"]["price_entries"] == 1

        order = client.get(f"{LAB}/orders/{reg['order_id']}", headers=lab.headers).json()
        assert order["study_type_id"] == str(lab.study_type.id) and order["order_code"] == reg["order_code"]
        st = client.get(f"{STUDY}{lab.study_type.id}", headers=lab.headers).json()
        assert st["is_removed"] is True and st["name"] == lab.study_type.name
        assert client.get(f"{STUDY}{lab.study_type.id}/report-defaults", headers=lab.headers).status_code == 200
        assert client.get(f"{LAB}/orders/{reg['order_id']}/report-defaults", headers=lab.headers).status_code == 200
        prices = client.get(PRICES, headers=lab.headers).json()["prices"]
        assert prices[0]["study_type"]["is_removed"] is True and prices[0]["study_type"]["code"] == "CU"

    def test_removal_touches_no_historical_row(self, client, session, lab):
        reg = register(client, lab, ("BX",))
        snapshot_sql = ('SELECT (SELECT md5(string_agg(o::text, \'|\' ORDER BY o.id)) FROM "order" o), '
                        "(SELECT md5(string_agg(s::text, '|' ORDER BY s.id)) FROM sample s), "
                        "(SELECT md5(string_agg(n::text, '|' ORDER BY n.id)) FROM numbering_allocation n), "
                        "(SELECT md5(string_agg(c::text, '|' ORDER BY c.scope_id)) FROM numbering_counter c), "
                        "(SELECT md5(coalesce(string_agg(r::text, '|' ORDER BY r.id), '')) FROM report r), "
                        "(SELECT count(*) FROM order_event)")
        before = session.execute(text(snapshot_sql)).one()
        remove(client, lab, "sample", lab.sample_types["BX"].id)
        remove(client, lab, "study", lab.study_type.id)
        session.expire_all()
        assert session.execute(text(snapshot_sql)).one() == before
        assert reg["order_code"]

    def test_removing_an_inactive_type_records_that_it_was_inactive(self, client, session, lab):
        bx = lab.sample_types["BX"]
        assert client.delete(f"{SAMPLE}{bx.id}", headers=lab.headers).status_code == 200
        r = remove(client, lab, "sample", bx.id)
        assert r.status_code == 200
        assert audit(session, "SAMPLE_TYPE.REMOVED", bx.id)[0][3]["was_active"] is False

    def test_reason_is_optional_and_bounded(self, client, lab):
        assert remove(client, lab, "sample", lab.sample_types["TJ"].id, reason="x" * 501).status_code == 422
        r = client.post(f"{SAMPLE}{lab.sample_types['TJ'].id}/remove", headers=lab.headers)  # no body at all
        assert r.status_code == 200 and r.json()["is_removed"] is True


# ---------------------------------------------------------------------------
# Deactivation stays what it was; a removed type is frozen
# ---------------------------------------------------------------------------


class TestInactiveVersusRemoved:
    @pytest.mark.parametrize("kind", ["study", "sample"])
    def test_deactivated_stays_in_administration_and_can_be_reactivated(self, client, lab, kind):
        row = type_row(lab, kind)
        assert client.delete(f"{BASES[kind]}{row.id}", headers=lab.headers).status_code == 200
        assert row.code not in listed(client, lab, kind)
        assert row.code in listed(client, lab, kind, active_only="false")
        back = client.put(f"{BASES[kind]}{row.id}", json={"is_active": True}, headers=lab.headers)
        assert back.status_code == 200 and back.json()["is_active"] is True and back.json()["is_removed"] is False

    @pytest.mark.parametrize("kind", ["study", "sample"])
    def test_a_removed_type_cannot_be_edited_reactivated_deactivated_or_erased(self, client, session, lab, kind):
        row = type_row(lab, kind)
        remove(client, lab, kind, row.id)
        table = "study_type" if kind == "study" else "sample_type"
        for attempt in (
            client.put(f"{BASES[kind]}{row.id}", json={"is_active": True}, headers=lab.headers),
            client.put(f"{BASES[kind]}{row.id}", json={"name": "Otro nombre"}, headers=lab.headers),
            client.delete(f"{BASES[kind]}{row.id}", headers=lab.headers),
            client.delete(f"{BASES[kind]}{row.id}", params={"hard_delete": "true"}, headers=lab.headers),
        ):
            assert attempt.status_code == 409, attempt.text
            assert attempt.json()["detail"]["code"] == f"{table}_removed"
        stored = session.execute(text(f"SELECT name, is_active, removed_at FROM {table} WHERE id = :i"),
                                 {"i": str(row.id)}).one()
        assert stored.name == row.name and stored.is_active is False and stored.removed_at is not None

    @pytest.mark.parametrize("table", ["study_type", "sample_type"])
    def test_the_database_refuses_an_active_removed_type(self, client, session, lab, table):
        row = lab.study_type if table == "study_type" else lab.sample_types["BX"]
        remove(client, lab, "study" if table == "study_type" else "sample", row.id)
        with pytest.raises(IntegrityError, match=f"ck_{table}_removed_inactive"):
            session.execute(text(f"UPDATE {table} SET is_active = true WHERE id = :i"), {"i": str(row.id)})
            session.commit()
        session.rollback()


# ---------------------------------------------------------------------------
# Authorization and tenant isolation
# ---------------------------------------------------------------------------


class TestAuthorization:
    @pytest.mark.parametrize("kind", ["study", "sample"])
    @pytest.mark.parametrize("role", ["viewer", "assistant"])
    def test_roles_without_manage_catalog_are_refused(self, client, session, lab, kind, role):
        row = type_row(lab, kind)
        r = remove(client, lab, kind, row.id, headers=lab.headers_for(role))
        assert r.status_code == 403
        assert row.code in listed(client, lab, kind)

    @pytest.mark.parametrize("kind", ["study", "sample"])
    def test_another_tenants_unknown_and_malformed_ids_are_not_found(self, client, session, lab, other, kind):
        foreign = type_row(other, kind)
        for type_id in (foreign.id, uuid.uuid4(), "not-a-uuid"):
            r = remove(client, lab, kind, type_id)
            assert r.status_code == 404, (type_id, r.text)
        assert foreign.code in listed(client, other, kind)
        table = "study_type" if kind == "study" else "sample_type"
        assert session.execute(text(f"SELECT removed_at FROM {table} WHERE id = :i"),
                               {"i": str(foreign.id)}).scalar_one() is None
        assert count(session, "SELECT count(*) FROM audit_log WHERE action LIKE '%.REMOVED'") == 0

    def test_removal_in_one_tenant_does_not_reach_another_with_the_same_code(self, client, lab, other):
        remove(client, lab, "sample", lab.sample_types["BX"].id)
        assert "BX" in listed(client, other, "sample")
        assert client.post(UNIFIED, json=other.unified(("BX",)), headers=other.headers).status_code == 200


# ---------------------------------------------------------------------------
# New records: API refusals on every creation path
# ---------------------------------------------------------------------------


class TestNewRecords:
    def test_a_removed_sample_type_is_refused_on_both_creation_routes(self, client, session, lab):
        reg = register(client, lab, ("TJ",))
        bx = lab.sample_types["BX"]
        remove(client, lab, "sample", bx.id)
        orders_before = count(session, 'SELECT count(*) FROM "order"')

        standalone = client.post(SAMPLES, json=lab.sample_payload(reg["order_id"], "BX"), headers=lab.headers)
        assert standalone.status_code == 400 and "removed" in standalone.json()["detail"]

        unified = client.post(UNIFIED, json=lab.unified(("TJ", "BX")), headers=lab.headers)
        assert unified.status_code == 400, unified.text
        detail = unified.json()["detail"]
        assert detail["code"] == "sample_type_removed" and detail["fields"] == ["samples.1.sample_type_id"]
        assert count(session, 'SELECT count(*) FROM "order"') == orders_before
        assert count(session, "SELECT count(*) FROM sample WHERE sample_type_id = :i", i=str(bx.id)) == 0

    def test_a_removed_study_type_is_refused_on_both_order_routes(self, client, session, lab):
        remove(client, lab, "study", lab.study_type.id)
        standalone = client.post(ORDERS, json=lab.order_payload(), headers=lab.headers)
        assert standalone.status_code == 400 and "removed" in standalone.json()["detail"]
        unified = client.post(UNIFIED, json=lab.unified(("BX",)), headers=lab.headers)
        assert unified.status_code == 400 and unified.json()["detail"]["code"] == "study_type_removed"
        assert count(session, 'SELECT count(*) FROM "order"') == 0

    def test_an_inactive_sample_type_keeps_its_own_refusal(self, client, lab):
        bx = lab.sample_types["BX"]
        client.delete(f"{SAMPLE}{bx.id}", headers=lab.headers)
        r = client.post(UNIFIED, json=lab.unified(("BX",)), headers=lab.headers)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "sample_type_inactive"

    def test_a_replay_after_removal_returns_the_original_records(self, client, session, lab):
        key = f"key-{uuid.uuid4().hex}"
        payload = lab.unified(("BX",))
        first = client.post(UNIFIED, json=payload, headers={**lab.headers, KEY: key})
        assert first.status_code == 200
        remove(client, lab, "sample", lab.sample_types["BX"].id)
        remove(client, lab, "study", lab.study_type.id)
        replay = client.post(UNIFIED, json=payload, headers={**lab.headers, KEY: key})
        assert replay.status_code == 200, replay.text
        assert replay.json()["order"]["id"] == first.json()["order"]["id"]
        assert count(session, 'SELECT count(*) FROM "order"') == 1

    def test_prices_cannot_be_created_for_or_moved_onto_a_removed_study_type(self, client, session, lab):
        spare = lab.add_study_type("OTRO")
        price = client.post(PRICES, json={"study_type_id": str(spare.id), "unit_price": 50}, headers=lab.headers).json()
        remove(client, lab, "study", lab.study_type.id)
        created = client.post(PRICES, json={"study_type_id": str(lab.study_type.id), "unit_price": 10}, headers=lab.headers)
        assert created.status_code == 409 and created.json()["detail"]["code"] == "study_type_removed"
        moved = client.put(f"{PRICES}{price['id']}", json={"study_type_id": str(lab.study_type.id)}, headers=lab.headers)
        assert moved.status_code == 409
        # A price that already belongs to the removed type stays editable (historical invoicing).
        own = PriceCatalog(tenant_id=lab.tenant.id, study_type_id=lab.study_type.id, unit_price=100)
        session.add(own)
        session.commit()
        edit = client.put(f"{PRICES}{own.id}", json={"unit_price": 120, "study_type_id": str(lab.study_type.id)},
                          headers=lab.headers)
        assert edit.status_code == 200, edit.text


# ---------------------------------------------------------------------------
# Unique codes stay reserved
# ---------------------------------------------------------------------------


class TestCodeReuse:
    """Owner decision 2026-11-25: unused → released for a distinct type; historical (withdrawn included) → reserved."""

    @pytest.mark.parametrize("kind", ["study", "sample"])
    def test_an_unused_removed_types_code_can_be_created_again_as_a_distinct_type(self, client, session, lab, kind):
        row = type_row(lab, kind)
        assert remove(client, lab, kind, row.id).json()["code_released"] is True
        again = client.post(BASES[kind], json={"code": row.code.lower(), "name": "Nuevo con el mismo código"},
                            headers=lab.headers)
        assert again.status_code == 200, again.text
        assert again.json()["id"] != str(row.id) and again.json()["code"] == row.code
        assert again.json()["is_active"] is True and again.json()["is_removed"] is False
        # The removed row keeps its original code, stays removed and is not remapped.
        table = "study_type" if kind == "study" else "sample_type"
        old = session.execute(text(f"SELECT code, is_active, removed_at, code_released_at FROM {table} WHERE id = :i"),
                              {"i": str(row.id)}).one()
        assert old.code == row.code and old.is_active is False and old.removed_at and old.code_released_at
        listed_codes = listed(client, lab, kind, active_only="false", include_removed="true")
        assert listed_codes.count(row.code) == 2
        # Only one type may hold it: a second claim collides with the NEW type, not the released one.
        third = client.post(BASES[kind], json={"code": row.code, "name": "Tercero"}, headers=lab.headers)
        assert third.status_code == 400 and "removed" not in third.json()["detail"]

    @pytest.mark.parametrize("kind", ["study", "sample"])
    def test_a_released_code_can_be_assigned_to_another_type_by_rename(self, client, lab, kind):
        row = type_row(lab, kind)
        remove(client, lab, kind, row.id)
        spare = client.post(BASES[kind], json={"code": "ZZ", "name": "Otro"}, headers=lab.headers).json()
        rename = client.put(f"{BASES[kind]}{spare['id']}", json={"code": row.code}, headers=lab.headers)
        assert rename.status_code == 200, rename.text
        assert rename.json()["code"] == row.code and rename.json()["id"] == spare["id"]

    @pytest.mark.parametrize("kind", ["study", "sample"])
    def test_a_historically_referenced_code_stays_reserved(self, client, session, lab, kind):
        register(client, lab, ("BX",))
        row = type_row(lab, kind)
        r = remove(client, lab, kind, row.id)
        assert r.json()["code_released"] is False
        again = client.post(BASES[kind], json={"code": row.code.lower(), "name": "Nuevo"}, headers=lab.headers)
        assert again.status_code == 400 and "reserved" in again.json()["detail"]
        spare = client.post(BASES[kind], json={"code": "ZZ", "name": "Otro"}, headers=lab.headers).json()
        rename = client.put(f"{BASES[kind]}{spare['id']}", json={"code": row.code}, headers=lab.headers)
        assert rename.status_code == 400 and "reserved" in rename.json()["detail"]
        table = "study_type" if kind == "study" else "sample_type"
        assert count(session, f"SELECT count(*) FROM {table} WHERE tenant_id = :t AND upper(code) = :c",
                     t=str(lab.tenant.id), c=row.code) == 1

    def test_a_withdrawn_sample_still_keeps_the_sample_type_code_reserved(self, client, session, lab):
        reg = register(client, lab, ("BX", "TJ"))
        w = client.post(f"{LAB}/samples/{reg['sample_ids'][0]}/removal", json={"reason": "captura duplicada"},
                        headers=lab.headers)
        assert w.status_code == 200, w.text
        bx = lab.sample_types["BX"]
        impact = client.get(f"{SAMPLE}{bx.id}/removal-impact", headers=lab.headers).json()
        assert impact["references"]["samples"] == 1 and impact["code_will_be_released"] is False
        assert remove(client, lab, "sample", bx.id).json()["code_released"] is False
        assert client.post(SAMPLE, json={"code": "BX", "name": "Nuevo"}, headers=lab.headers).status_code == 400

    def test_a_withdrawn_order_still_keeps_the_study_type_code_reserved(self, client, session, lab):
        reg = register(client, lab, ("BX",))
        w = client.post(f"{LAB}/orders/{reg['order_id']}/removal", json={"reason": "orden capturada por error"},
                        headers=lab.headers)
        assert w.status_code == 200, w.text
        r = remove(client, lab, "study", lab.study_type.id)
        assert r.json()["references"]["orders"] == 1 and r.json()["code_released"] is False
        assert client.post(STUDY, json={"code": "CU", "name": "Nuevo"}, headers=lab.headers).status_code == 400
        # The sample type of that withdrawn order is historical too.
        assert remove(client, lab, "sample", lab.sample_types["BX"].id).json()["code_released"] is False

    def test_prices_alone_do_not_keep_a_study_type_code(self, client, session, lab):
        session.add(PriceCatalog(tenant_id=lab.tenant.id, study_type_id=lab.study_type.id, unit_price=10))
        session.commit()
        r = remove(client, lab, "study", lab.study_type.id).json()
        assert r["references"]["price_entries"] == 1 and r["code_released"] is True
        new = client.post(STUDY, json={"code": "CU", "name": "Nuevo"}, headers=lab.headers).json()
        # the price stays with the removed type; the new type inherits nothing
        prices = client.get(PRICES, params={"active_only": "false"}, headers=lab.headers).json()["prices"]
        assert [p["study_type_id"] for p in prices] == [str(lab.study_type.id)] and new["id"] != str(lab.study_type.id)

    def test_catalogs_and_tenants_are_independent_namespaces(self, client, lab, other):
        register(client, lab, ("BX",))          # BX (sample) and CU (study) are historical in Alfa
        lab.add_sample_type("CU")                # a SAMPLE type with the study type's code
        remove(client, lab, "study", lab.study_type.id)
        cu_sample = lab.sample_types["CU"]
        assert remove(client, lab, "sample", cu_sample.id).json()["code_released"] is True
        assert client.post(SAMPLE, json={"code": "CU", "name": "Otra"}, headers=lab.headers).status_code == 200
        assert client.post(STUDY, json={"code": "CU", "name": "Otro"}, headers=lab.headers).status_code == 400
        # another tenant is unaffected either way
        assert client.post(STUDY, json={"code": "NEW", "name": "N"}, headers=other.headers).status_code == 200
        assert "BX" in listed(client, other, "sample")

    def test_removal_impact_previews_the_decision_and_is_permissioned(self, client, lab, other):
        bx = lab.sample_types["BX"]
        impact = client.get(f"{SAMPLE}{bx.id}/removal-impact", headers=lab.headers)
        assert impact.status_code == 200 and impact.json()["code_will_be_released"] is True
        assert client.get(f"{SAMPLE}{bx.id}/removal-impact", headers=lab.headers_for("viewer")).status_code == 403
        assert client.get(f"{SAMPLE}{bx.id}/removal-impact", headers=other.headers).status_code == 404
        st = client.get(f"{STUDY}{lab.study_type.id}/removal-impact", headers=lab.headers).json()
        assert st["references"]["orders"] == 0 and st["code_will_be_released"] is True

    def test_the_database_allows_one_reserving_row_per_code(self, client, session, lab):
        bx = lab.sample_types["BX"]
        remove(client, lab, "sample", bx.id)
        client.post(SAMPLE, json={"code": "BX", "name": "Nuevo"}, headers=lab.headers)
        with pytest.raises(IntegrityError, match="uq_sample_type_tenant_code_reserved"):
            session.execute(text("UPDATE sample_type SET code_released_at = NULL WHERE id = :i"), {"i": str(bx.id)})
            session.commit()
        session.rollback()
        with pytest.raises(IntegrityError, match="ck_sample_type_code_release_removed"):
            session.execute(text("UPDATE sample_type SET code_released_at = now() WHERE id = :i"),
                            {"i": str(lab.sample_types["TJ"].id)})
            session.commit()
        session.rollback()

    @pytest.mark.parametrize("kind", ["study", "sample"])
    def test_removal_racing_a_registration_never_frees_a_used_code(self, fresh_client, session, lab, kind):
        row = type_row(lab, kind)
        payload = lab.unified(("BX",))
        results = run_parallel([
            lambda c: c.post(UNIFIED, json=payload, headers={**lab.headers, KEY: f"k-{uuid.uuid4().hex}"}),
            lambda c: c.post(f"{BASES[kind]}{row.id}/remove", headers=lab.headers),
        ])
        registered, removed = results
        assert removed.status_code == 200
        table = "study_type" if kind == "study" else "sample_type"
        released = session.execute(text(f"SELECT code_released_at IS NOT NULL FROM {table} WHERE id = :i"),
                                   {"i": str(row.id)}).scalar_one()
        if registered.status_code == 200:   # registration first: the type has history → reserved
            assert released is False and removed.json()["code_released"] is False
        else:                               # removal first: registration refused, nothing used it
            assert registered.status_code == 400 and released is True

    @pytest.mark.parametrize("kind", ["study", "sample"])
    def test_removal_racing_a_create_with_the_same_code_leaves_one_holder(self, fresh_client, session, lab, kind):
        row = type_row(lab, kind)
        results = run_parallel([
            lambda c: c.post(f"{BASES[kind]}{row.id}/remove", headers=lab.headers),
            lambda c: c.post(BASES[kind], json={"code": row.code, "name": "Carrera"}, headers=lab.headers),
        ])
        assert results[0].status_code == 200 and results[1].status_code in (200, 400)
        table = "study_type" if kind == "study" else "sample_type"
        holders = count(session, f"SELECT count(*) FROM {table} WHERE tenant_id = :t AND code = :c "
                                 "AND code_released_at IS NULL", t=str(lab.tenant.id), c=row.code)
        assert holders == (1 if results[1].status_code == 200 else 0)


# ---------------------------------------------------------------------------
# Export, import and copy
# ---------------------------------------------------------------------------


class TestTransfer:
    @pytest.mark.parametrize("kind", ["study", "sample"])
    def test_export_leaves_removed_types_out(self, client, lab, kind):
        row = type_row(lab, kind)
        remove(client, lab, kind, row.id)
        for include_inactive in ("true", "false"):
            document = client.get(f"{BASES[kind]}export", params={"include_inactive": include_inactive},
                                  headers=lab.headers).json()
            assert row.code not in [i["code"] for i in document["items"]]

    def test_import_reports_a_reserved_removed_code_as_taken_and_never_recreates_it(self, client, session, lab):
        register(client, lab, ("BX",))  # history keeps BX reserved
        remove(client, lab, "sample", lab.sample_types["BX"].id)
        document = {"schema": "celuma.catalog-export", "version": 1, "catalog_kind": "sample_types",
                    "items": [{"code": "bx", "name": "Biopsia"}, {"code": "NEW", "name": "Nuevo"}]}
        plan = client.post(f"{SAMPLE}import/preview", json={"document": document}, headers=lab.headers).json()
        first = plan["items"][0]
        assert first["status"] == "code_exists" and "removed" in first["issues"][0]["message"]
        assert first["existing_sample_type_id"] == str(lab.sample_types["BX"].id)
        abort = client.post(f"{SAMPLE}import/confirm", headers=lab.headers, json={
            "document": document, "selected_indexes": [0, 1], "plan_digest": plan["plan_digest"]})
        assert abort.status_code == 409
        skip = client.post(f"{SAMPLE}import/confirm", headers=lab.headers, json={
            "document": document, "selected_indexes": [0, 1], "plan_digest": plan["plan_digest"], "on_conflict": "skip"})
        assert skip.status_code == 200 and [c["code"] for c in skip.json()["created"]] == ["NEW"]
        removed = session.execute(text("SELECT removed_at, is_active FROM sample_type WHERE id = :i"),
                                  {"i": str(lab.sample_types["BX"].id)}).one()
        assert removed.removed_at is not None and removed.is_active is False

    def test_import_and_copy_may_use_a_released_code_for_a_new_type(self, client, session, lab):
        bx = lab.sample_types["BX"]
        remove(client, lab, "sample", bx.id)
        document = {"schema": "celuma.catalog-export", "version": 1, "catalog_kind": "sample_types",
                    "items": [{"code": "bx", "name": "Biopsia nueva"}]}
        plan = client.post(f"{SAMPLE}import/preview", json={"document": document}, headers=lab.headers).json()
        assert plan["items"][0]["status"] == "ready"
        done = client.post(f"{SAMPLE}import/confirm", headers=lab.headers, json={
            "document": document, "selected_indexes": [0], "plan_digest": plan["plan_digest"]})
        assert done.status_code == 200 and done.json()["created"][0]["id"] != str(bx.id)
        old = session.execute(text("SELECT removed_at, is_active FROM sample_type WHERE id = :i"), {"i": str(bx.id)}).one()
        assert old.removed_at is not None and old.is_active is False
        # copy: a study type whose code was released as a SAMPLE type copies into a new sample type
        tj = lab.sample_types["TJ"]
        remove(client, lab, "sample", tj.id)
        st = lab.add_study_type("TJ")
        copy = client.post(f"{SAMPLE}copy-from-study-types/preview", json={"study_type_ids": [str(st.id)]},
                           headers=lab.headers).json()
        assert copy["items"][0]["status"] == "ready"

    def test_a_removed_study_type_cannot_be_copied(self, client, session, lab):
        extra = lab.add_study_type("HIS")
        remove(client, lab, "study", lab.study_type.id)
        r = client.post(f"{SAMPLE}copy-from-study-types/preview",
                        json={"study_type_ids": [str(extra.id), str(lab.study_type.id)]}, headers=lab.headers)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "study_type_removed"
        assert client.post(f"{SAMPLE}copy-from-study-types/preview", json={"study_type_ids": [str(extra.id)]},
                           headers=lab.headers).status_code == 200


# ---------------------------------------------------------------------------
# #64 numbering: retained, frozen, never reset
# ---------------------------------------------------------------------------


class TestNumbering:
    def type_url(self, kind, type_id):
        return f"{NUMBERING}/type-configs/{kind}/{type_id}"

    def test_a_removed_types_configuration_is_kept_frozen_and_unlisted(self, client, session, lab):
        tj = lab.sample_types["TJ"]
        fmt = {"format": "{BASE}-{SEQ}{MM}{YY}", "sequence_min_width": 2, "start_value": 1}
        assert client.put(self.type_url("sample", tj.id), json=fmt, headers=lab.headers).status_code == 200
        reg = register(client, lab, ("TJ",))
        assert reg["sample_codes"] == ["TJ-011026"]
        counters = count(session, "SELECT count(*) FROM numbering_counter WHERE scope_id = :i", i=str(tj.id))
        remove(client, lab, "sample", tj.id)

        listing = client.get(f"{NUMBERING}/type-configs/sample", headers=lab.headers).json()
        assert "TJ" not in [t["code"] for t in listing["types"]]
        for attempt in (client.put(self.type_url("sample", tj.id), json=fmt, headers=lab.headers),
                        client.delete(self.type_url("sample", tj.id), headers=lab.headers)):
            assert attempt.status_code == 409 and attempt.json()["detail"]["code"] == "sample_type_removed"
        assert count(session, "SELECT count(*) FROM numbering_type_config WHERE type_id = :i", i=str(tj.id)) == 1
        assert count(session, "SELECT count(*) FROM numbering_counter WHERE scope_id = :i", i=str(tj.id)) == counters

        # The receipt-date regeneration of an EXISTING sample still uses the retained configuration.
        r = client.patch(f"{LAB}/samples/{reg['sample_ids'][0]}/dates",
                         json={"received_at": "2026-09-28", "reason": "llegó en septiembre"}, headers=lab.headers)
        assert r.status_code == 200, r.text
        assert r.json()["sample_code"] == "TJ-010926"

    def test_a_removed_types_configuration_never_blocks_a_laboratory_scope_change(self, client, lab):
        tj = lab.sample_types["TJ"]
        client.put(self.type_url("sample", tj.id), json={"format": "{BASE}-{SEQ}{MM}{YY}", "sequence_min_width": 2,
                                                          "start_value": 1}, headers=lab.headers)
        current = client.get(f"{NUMBERING}/config", headers=lab.headers).json()["sample"]
        body = {k: current[k] for k in ("active", "format", "sequence_min_width", "start_value", "timezone")}
        body.update(scope="branch", format="{BASE}{BRANCH}{SEQ}{MM}{YY}")
        assert client.put(f"{NUMBERING}/config/sample", json=body, headers=lab.headers).status_code == 422
        remove(client, lab, "sample", tj.id)
        r = client.put(f"{NUMBERING}/config/sample", json=body, headers=lab.headers)
        assert r.status_code == 200, r.text

    def test_a_reused_code_never_reissues_a_code_and_inherits_no_configuration(self, client, session, lab):
        """An operator-deleted history (rows gone, ledger kept) frees the code, but the ledger still blocks
        every code it issued; the new type starts with the laboratory's numbering, not the old override."""
        tj = lab.sample_types["TJ"]
        client.put(self.type_url("sample", tj.id), json={"format": "{BASE}-{SEQ}{MM}{YY}", "sequence_min_width": 2,
                                                          "start_value": 1}, headers=lab.headers)
        reg = register(client, lab, ("TJ",))
        assert reg["sample_codes"] == ["TJ-011026"]
        delete_order_and_samples(session, reg["order_id"])  # rows gone, ledger + counter + config stay
        assert remove(client, lab, "sample", tj.id).json()["code_released"] is True
        new = client.post(SAMPLE, json={"code": "TJ", "name": "Tejido nuevo"}, headers=lab.headers).json()
        listing = {t["code"]: t for t in client.get(f"{NUMBERING}/type-configs/sample", headers=lab.headers).json()["types"]}
        assert listing["TJ"]["type_id"] == new["id"] and listing["TJ"]["source"] == "laboratory"
        assert count(session, "SELECT count(*) FROM numbering_type_config WHERE type_id = :i", i=str(tj.id)) == 1
        lab.sample_types["TJ"] = session.get(SampleType, uuid.UUID(new["id"]))
        codes = [register(client, lab, ("TJ",))["sample_codes"][0] for _ in range(2)]
        assert codes == ["TJ011026", "TJ021026"]  # laboratory format; distinct counter
        issued = [r[0] for r in session.execute(text(
            "SELECT code FROM numbering_allocation WHERE entity_kind = 'sample' ORDER BY code"))]
        assert len(issued) == len(set(issued)) and "TJ-011026" in issued

    def test_a_reused_study_code_skips_order_codes_already_issued(self, client, session, lab):
        reg = register(client, lab, ("BX",))
        assert reg["order_code"] == "CU011026"
        delete_order_and_samples(session, reg["order_id"])
        assert remove(client, lab, "study", lab.study_type.id).json()["code_released"] is True
        new = lab.add_study_type("CU")
        r = client.post(UNIFIED, json=lab.unified(("BX",), study_type=new), headers={**lab.headers, KEY: f"k-{uuid.uuid4().hex}"})
        assert r.status_code == 200, r.text
        assert r.json()["order"]["order_code"] == "CU021026"  # CU011026 is in the ledger: never reissued

    def test_a_removed_study_types_order_numbering_is_frozen(self, client, lab):
        remove(client, lab, "study", lab.study_type.id)
        r = client.put(self.type_url("order", lab.study_type.id),
                       json={"format": "{BASE}-{SEQ}{MM}{YY}", "sequence_min_width": 2, "start_value": 1},
                       headers=lab.headers)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "study_type_removed"


def test_removed_rows_stay_resolvable_through_the_orm(session, lab):
    """No global filter (unlike #11 orders/samples): historical reads use `Session.get`."""
    lab.study_type.removed_at = lab.study_type.created_at
    lab.study_type.is_active = False
    session.add(lab.study_type)
    session.commit()
    session.expire_all()
    assert session.get(StudyType, lab.study_type.id) is not None
    assert session.get(SampleType, lab.sample_types["BX"].id) is not None
