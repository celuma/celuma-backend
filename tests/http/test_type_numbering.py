"""Optional numbering per study type and per sample type over HTTP (Céluma 1.5, issue #64).

Contract: celuma-engineering/specs/laboratory/type-numbering.md. An order resolves its study type's own
configuration, a sample its own sample type's; without one the laboratory's applies. Only format, minimum
width and start value vary; activation, scope and timezone are the laboratory's; the counter key is the
type id, so switching configuration continues the sequence. Real PostgreSQL; synthetic data only.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from .management_support import LAB, register
from .numbering_support import (  # noqa: F401
    NUMBERING, ORDERS, SAMPLES, UNIFIED, Lab, configure, counter_rows, freeze, fresh_client, ledger_rows,
    order_codes, run_parallel, sample_codes, set_counter,
)

KEY = "Idempotency-Key"
HYPHEN = "{BASE}-{SEQ}{MM}{YY}"


@pytest.fixture
def lab(session, monkeypatch):
    freeze(monkeypatch)  # 2026-10-15 18:00 UTC: October in Mexico City
    lab = Lab(session, "Alfa", sample_types=("BX", "TJ"))
    configure(session, lab)
    return lab


def type_url(kind, type_id):
    return f"{NUMBERING}/type-configs/{kind}/{type_id}"


def set_type(client, lab, kind, type_row, fmt=HYPHEN, width=2, start=1, headers=None):
    r = client.put(type_url(kind, type_row.id), json={"format": fmt, "sequence_min_width": width, "start_value": start},
                   headers=headers or lab.headers)
    return r


def clear_type(client, lab, kind, type_row, headers=None):
    return client.delete(type_url(kind, type_row.id), headers=headers or lab.headers)


def order(client, lab, study_type=None):
    r = client.post(ORDERS, json=lab.order_payload(study_type), headers=lab.headers)
    assert r.status_code == 200, r.text
    return r.json()


def sample(client, lab, order_id, type_code="BX", received="2026-10-15"):
    r = client.post(SAMPLES, json=lab.sample_payload(order_id, type_code, received_at=received), headers=lab.headers)
    assert r.status_code == 200, r.text
    return r.json()


def preview(client, lab, kind, type_row, **extra):
    r = client.post(f"{NUMBERING}/preview", json={"entity_kind": kind, "scope_id": str(type_row.id), **extra},
                    headers=lab.headers)
    assert r.status_code == 200, r.text
    return r.json()


def audit_actions(session):
    return [r[0] for r in session.execute(text(
        "SELECT action FROM audit_log WHERE action LIKE 'NUMBERING.TYPE_CONFIG_%' ORDER BY created_at"))]


class TestResolution:
    def test_a_type_without_its_own_configuration_uses_the_laboratory_default(self, client, session, lab):
        o = order(client, lab)
        assert o["order_code"] == "CU011026"
        assert sample(client, lab, o["id"])["sample_code"] == "BX011026"
        assert session.execute(text("SELECT count(*) FROM numbering_type_config")).scalar() == 0

    def test_a_study_type_override_numbers_its_orders_and_leaves_other_types_alone(self, client, session, lab):
        other = lab.add_study_type("HI")
        assert set_type(client, lab, "order", lab.study_type, width=3).status_code == 200
        assert order(client, lab)["order_code"] == "CU-0011026"
        assert order(client, lab, other)["order_code"] == "HI011026"
        fmt = session.execute(text("SELECT format, sequence_min_width FROM numbering_allocation WHERE code = 'CU-0011026'")).one()
        assert tuple(fmt) == (HYPHEN, 3)  # the ledger records what produced the code

    def test_mixed_sample_types_in_one_unified_request_resolve_independently(self, client, session, lab):
        set_type(client, lab, "sample", lab.sample_types["TJ"], fmt="{BASE}.{SEQ}{MM}{YYYY}", width=3)
        set_type(client, lab, "order", lab.study_type, fmt=HYPHEN)
        reg = register(client, lab, ("BX", "TJ", "BX", "TJ"))
        assert reg["order_code"] == "CU-011026"
        assert reg["sample_codes"] == ["BX011026", "TJ.001102026", "BX021026", "TJ.002102026"]

    def test_standalone_sample_uses_its_own_type_not_the_order(self, client, session, lab):
        set_type(client, lab, "sample", lab.sample_types["TJ"])
        o = order(client, lab)
        assert sample(client, lab, o["id"], "TJ")["sample_code"] == "TJ-011026"
        assert sample(client, lab, o["id"], "BX")["sample_code"] == "BX011026"

    def test_preview_and_suggestions_resolve_exactly_like_allocation(self, client, session, lab):
        set_type(client, lab, "order", lab.study_type, width=4)
        set_type(client, lab, "sample", lab.sample_types["TJ"])
        counters, ledger = counter_rows(session), ledger_rows(session)
        p = preview(client, lab, "order", lab.study_type)
        assert p["config_source"] == "type" and p["estimate"]["code"] == "CU-00011026" and p["estimate"]["reserved"] is False
        lab_layer = preview(client, lab, "order", lab.study_type, layer="laboratory")
        assert lab_layer["config_source"] == "laboratory" and lab_layer["estimate"]["code"] == "CU011026"
        sug = client.post(f"{NUMBERING}/sample-code-suggestions", json={
            "branch_id": str(lab.branch.id),
            "items": [{"key": k, "sample_type_id": str(lab.sample_types[t].id), "received_at": "2026-10-15"}
                      for k, t in (("a", "BX"), ("b", "TJ"), ("c", "TJ"))],
        }, headers=lab.headers).json()["suggestions"]
        assert [s["code"] for s in sug] == ["BX011026", "TJ-011026", "TJ-021026"]
        # Nothing reserved or written by previews/suggestions.
        assert (counter_rows(session), ledger_rows(session)) == (counters, ledger)
        reg = register(client, lab, ("BX", "TJ", "TJ"))
        assert reg["order_code"] == p["estimate"]["code"]
        assert reg["sample_codes"] == [s["code"] for s in sug]

    def test_candidate_fields_in_the_type_editor_preview_apply_over_the_type(self, client, session, lab):
        set_type(client, lab, "order", lab.study_type, width=4)
        p = preview(client, lab, "order", lab.study_type, sequence_min_width=2)
        assert p["format"] == HYPHEN and p["estimate"]["code"] == "CU-011026"

    def test_inactive_kind_ignores_type_configurations(self, client, session, monkeypatch):
        freeze(monkeypatch)
        lab = Lab(session, "Beta", sample_types=("BX",))  # never activated: compatibility numbering
        assert set_type(client, lab, "order", lab.study_type).status_code == 200
        assert order(client, lab)["order_code"] == "CU-1"
        assert preview(client, lab, "order", lab.study_type)["scheme"] == "legacy"
        listed = client.get(f"{NUMBERING}/type-configs/order", headers=lab.headers).json()
        assert listed["laboratory"]["active"] is False and listed["types"][0]["source"] == "type"
        assert listed["types"][0]["effective"]["scheme"] == "legacy"


class TestContinuity:
    def test_set_change_and_clear_continue_the_same_counter(self, client, session, lab):
        assert [order(client, lab)["order_code"] for _ in range(2)] == ["CU011026", "CU021026"]
        set_type(client, lab, "order", lab.study_type)
        assert order(client, lab)["order_code"] == "CU-031026"
        set_type(client, lab, "order", lab.study_type, width=4)
        assert order(client, lab)["order_code"] == "CU-00041026"
        assert clear_type(client, lab, "order", lab.study_type).status_code == 200
        assert order(client, lab)["order_code"] == "CU051026"
        assert session.execute(text(
            "SELECT last_value FROM numbering_counter WHERE entity_kind = 'order'")).scalar() == 5

    def test_start_value_only_moves_forward_and_clearing_never_rewinds(self, client, session, lab):
        order(client, lab)
        set_type(client, lab, "order", lab.study_type, start=500)
        assert order(client, lab)["order_code"] == "CU-5001026"
        clear_type(client, lab, "order", lab.study_type)
        assert order(client, lab)["order_code"] == "CU5011026"
        set_type(client, lab, "order", lab.study_type, start=3)  # lower: nothing reached is rewound
        assert order(client, lab)["order_code"] == "CU-5021026"

    def test_a_string_issued_under_another_configuration_is_skipped_never_reused(self, client, session, lab):
        # Type BX with a format that renders exactly what TJ would: the ledger/live-row guard skips it.
        set_type(client, lab, "sample", lab.sample_types["BX"], fmt="TJ{BASE}{SEQ}{MM}{YY}")
        lab.add_sample_type("TJBX")
        o = order(client, lab)
        first = sample(client, lab, o["id"], "BX")["sample_code"]
        assert first == "TJBX011026"
        assert sample(client, lab, o["id"], "TJBX")["sample_code"] == "TJBX021026"  # 01 occupied → skipped

    def test_month_boundary_follows_the_receipt_date_with_an_override(self, client, session, lab):
        set_type(client, lab, "sample", lab.sample_types["TJ"])
        o = order(client, lab)
        codes = [sample(client, lab, o["id"], "TJ", d)["sample_code"] for d in ("2026-09-30", "2026-10-01", "2026-09-30")]
        assert codes == ["TJ-010926", "TJ-011026", "TJ-020926"]
        assert order(client, lab)["order_code"] == "CU021026"  # orders keep the allocation instant

    def test_receipt_date_regeneration_uses_the_types_configuration(self, client, session, lab):
        set_type(client, lab, "sample", lab.sample_types["TJ"])
        reg = register(client, lab, ("TJ",))
        assert reg["sample_codes"] == ["TJ-011026"]
        r = client.patch(f"{LAB}/samples/{reg['sample_ids'][0]}/dates",
                         json={"received_at": "2026-09-28", "reason": "llegó en septiembre"}, headers=lab.headers)
        assert r.status_code == 200, r.text
        assert r.json()["sample_code"] == "TJ-010926"


class TestRetriesAndConcurrency:
    def test_a_replay_after_a_configuration_change_returns_the_original_codes(self, client, session, lab):
        key = f"key-{uuid.uuid4().hex}"
        payload = lab.unified(("BX", "TJ"))
        first = client.post(UNIFIED, json=payload, headers={**lab.headers, KEY: key})
        assert first.status_code == 200
        set_type(client, lab, "order", lab.study_type)
        set_type(client, lab, "sample", lab.sample_types["TJ"])
        ledger = ledger_rows(session)
        again = client.post(UNIFIED, json=payload, headers={**lab.headers, KEY: key})
        assert again.status_code == 200 and again.headers.get("Idempotent-Replayed") == "true"
        assert again.json()["order"]["order_code"] == "CU011026"
        assert [s["sample_code"] for s in again.json()["samples"]] == ["BX011026", "TJ011026"]
        assert ledger_rows(session) == ledger

    def test_concurrent_unified_requests_with_overrides_get_unique_contiguous_codes(self, fresh_client, session, lab):
        set_type(fresh_client, lab, "order", lab.study_type)
        set_type(fresh_client, lab, "sample", lab.sample_types["TJ"], width=3)
        jobs = [
            (lambda c, kinds=(("BX", "TJ") if i % 2 == 0 else ("TJ", "BX")): c.post(
                UNIFIED, json=lab.unified(sample_codes=kinds), headers=lab.headers))
            for i in range(10)
        ]
        results = run_parallel(jobs)
        bad = [r for r in results if isinstance(r, Exception) or r.status_code != 200]
        assert not bad, [getattr(r, "text", r) for r in bad]
        orders = sorted(r.json()["order"]["order_code"] for r in results)
        assert orders == sorted(f"CU-{n:02d}1026" for n in range(1, 11))
        samples = sorted(s["sample_code"] for r in results for s in r.json()["samples"])
        assert samples == sorted([f"BX{n:02d}1026" for n in range(1, 11)] + [f"TJ-{n:03d}1026" for n in range(1, 11)])

    def test_configuration_switch_between_parallel_batches_never_duplicates(self, fresh_client, session, lab):
        def batch(n):
            results = run_parallel([lambda c: c.post(ORDERS, json=lab.order_payload(), headers=lab.headers)] * n)
            assert all(not isinstance(r, Exception) and r.status_code == 200 for r in results)
            return [r.json()["order_code"] for r in results]
        a = batch(5)
        set_type(fresh_client, lab, "order", lab.study_type)
        b = batch(5)
        clear_type(fresh_client, lab, "order", lab.study_type)
        c = batch(5)
        assert sorted(a) == [f"CU{n:02d}1026" for n in range(1, 6)]
        assert sorted(b) == [f"CU-{n:02d}1026" for n in range(6, 11)]
        assert sorted(c) == [f"CU{n:02d}1026" for n in range(11, 16)]


class TestAdministration:
    def test_list_shows_inherited_and_own_settings(self, client, session, lab):
        set_type(client, lab, "sample", lab.sample_types["TJ"], width=3, start=7)
        body = client.get(f"{NUMBERING}/type-configs/sample", headers=lab.headers).json()
        assert body["laboratory"]["format"] == "{BASE}{SEQ}{MM}{YY}"
        rows = {t["code"]: t for t in body["types"]}
        assert rows["BX"]["source"] == "laboratory" and rows["BX"]["override"] is None
        assert rows["BX"]["effective"]["format"] == "{BASE}{SEQ}{MM}{YY}"
        assert rows["TJ"]["source"] == "type" and rows["TJ"]["override"]["start_value"] == 7
        assert rows["TJ"]["effective"] == {"format": HYPHEN, "sequence_min_width": 3, "start_value": 7, "active": True,
                                           "scope": "tenant", "timezone": "America/Mexico_City", "scheme": "monthly"}

    def test_set_and_clear_are_audited_and_clearing_twice_is_a_no_op(self, client, session, lab):
        set_type(client, lab, "order", lab.study_type)
        set_type(client, lab, "order", lab.study_type, width=3)
        assert clear_type(client, lab, "order", lab.study_type).json()["source"] == "laboratory"
        assert clear_type(client, lab, "order", lab.study_type).status_code == 200
        assert audit_actions(session) == ["NUMBERING.TYPE_CONFIG_SET", "NUMBERING.TYPE_CONFIG_SET", "NUMBERING.TYPE_CONFIG_CLEARED"]
        row = session.execute(text(
            "SELECT old_values, new_values, entity_id FROM audit_log WHERE action = 'NUMBERING.TYPE_CONFIG_SET' "
            "ORDER BY created_at DESC LIMIT 1")).one()
        assert row.old_values["sequence_min_width"] == 2 and row.new_values["sequence_min_width"] == 3
        assert str(row.entity_id) == str(lab.study_type.id)

    @pytest.mark.parametrize("body,fragment", [
        ({"format": "{BASE}{SEQ}", "sequence_min_width": 2, "start_value": 1}, "{MM}"),
        ({"format": "{BASE}{BRANCH}{SEQ}{MM}{YY}", "sequence_min_width": 2, "start_value": 1}, "{BRANCH}"),
        ({"format": HYPHEN, "sequence_min_width": 9, "start_value": 1}, "sequence_min_width"),
        ({"format": HYPHEN, "sequence_min_width": 2, "start_value": 0}, "start_value"),
        ({"format": "{BASE}{SEQ}{MM}{YY}" + "X" * 52, "sequence_min_width": 6, "start_value": 1}, None),
    ])
    def test_invalid_configurations_are_refused_and_nothing_is_written(self, client, session, lab, body, fragment):
        r = client.put(type_url("order", lab.study_type.id), json=body, headers=lab.headers)
        assert r.status_code == 422, r.text
        if fragment:
            assert fragment in r.text
        assert session.execute(text("SELECT count(*) FROM numbering_type_config")).scalar() == 0
        assert audit_actions(session) == []

    def test_a_rendered_code_too_long_for_the_type_is_refused(self, client, session, lab):
        long_type = lab.add_study_type("L" * 50)
        r = set_type(client, lab, "order", long_type, fmt="{BASE}{SEQ}{MM}{YYYY}" + "Z" * 39, width=6)
        assert r.status_code == 422 and "96" in r.text

    def test_permissions_reads_need_lab_read_and_writes_need_manage_catalog(self, client, session, lab):
        for role in ("lab_tech", "viewer", "billing", "assistant"):
            h = lab.headers_for(role)
            assert set_type(client, lab, "order", lab.study_type, headers=h).status_code == 403
            assert clear_type(client, lab, "order", lab.study_type, headers=h).status_code == 403
        assert set_type(client, lab, "order", lab.study_type, headers=lab.headers_for("admin")).status_code == 200
        assert client.get(f"{NUMBERING}/type-configs/order", headers=lab.headers_for("lab_tech")).status_code == 200

    def test_tenant_isolation_on_configuration_and_allocation(self, client, session, lab, monkeypatch):
        other = Lab(session, "Gama", sample_types=("BX",))
        configure(session, other)
        # Another tenant's type is 404 (not 403) for every route, and nothing is written.
        assert set_type(client, lab, "order", other.study_type).status_code == 404
        assert clear_type(client, lab, "order", other.study_type).status_code == 404
        assert client.post(f"{NUMBERING}/preview", json={"entity_kind": "order", "scope_id": str(other.study_type.id)},
                           headers=lab.headers).status_code == 404
        assert client.put(type_url("order", "not-a-uuid"), json={"format": HYPHEN, "sequence_min_width": 2},
                          headers=lab.headers).status_code == 404
        # Alfa's override never reaches Gama, even for a type with the same code.
        set_type(client, lab, "order", lab.study_type)
        assert order(client, other)["order_code"] == "CU011026"
        listed = client.get(f"{NUMBERING}/type-configs/order", headers=other.headers).json()
        assert [t["source"] for t in listed["types"]] == ["laboratory"]

    def test_laboratory_scope_change_is_refused_while_a_type_format_would_break(self, client, session, lab):
        set_type(client, lab, "order", lab.study_type)
        body = {"active": True, "format": "{BASE}{BRANCH}{SEQ}{MM}{YY}", "sequence_min_width": 2, "start_value": 1,
                "scope": "branch", "timezone": "America/Mexico_City"}
        r = client.put(f"{NUMBERING}/config/order", json=body, headers=lab.headers)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "numbering_type_config_conflict"
        assert r.json()["detail"]["types"] == ["CU"]
        assert session.execute(text("SELECT scope FROM numbering_config WHERE entity_kind = 'order'")).scalar() == "tenant"
        clear_type(client, lab, "order", lab.study_type)
        assert client.put(f"{NUMBERING}/config/order", json=body, headers=lab.headers).status_code == 200
        # Under branch scope a type format must carry {BRANCH}.
        assert set_type(client, lab, "order", lab.study_type).status_code == 422
        assert set_type(client, lab, "order", lab.study_type, fmt="{BASE}-{BRANCH}-{SEQ}{MM}{YY}").status_code == 200

    def test_hard_deleting_a_type_removes_its_configuration(self, client, session, lab):
        spare = lab.add_sample_type("ZZ")
        set_type(client, lab, "sample", spare)
        spare_study = lab.add_study_type("ZS")
        set_type(client, lab, "order", spare_study)
        assert client.delete(f"/api/v1/sample-types/{spare.id}?hard_delete=true", headers=lab.headers).status_code == 200
        assert client.delete(f"/api/v1/study-types/{spare_study.id}?hard_delete=true", headers=lab.headers).status_code == 200
        assert session.execute(text("SELECT count(*) FROM numbering_type_config")).scalar() == 0
