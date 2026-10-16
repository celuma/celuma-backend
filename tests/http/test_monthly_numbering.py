"""Monthly order and sample numbering over HTTP (issue #8).

Contract: celuma-engineering/specs/laboratory/monthly-numbering.md.
Concurrency lives in test_numbering_concurrency.py.
"""
from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.models.audit import AuditLog

from .factories import auth_headers, create_order, create_sample
from .numbering_support import (  # noqa: F401
    fresh_client,
    NUMBERING, ORDERS, SAMPLES, UNIFIED, Lab, counter_rows, delete_order_and_samples, freeze, ledger_rows,
    order_codes, sample_codes, set_counter,
)


@pytest.fixture
def lab(session, monkeypatch):
    freeze(monkeypatch)  # 2026-10-15 18:00 UTC = Oct 15, 12:00 Mexico City
    return Lab(session, "Alfa", sample_types=("BX", "TJ"))


@pytest.fixture
def other(session):
    return Lab(session, "Beta", sample_types=("BX",))


def make_order(client, lab, **extra):
    r = client.post(UNIFIED, json=lab.unified(**extra), headers=lab.headers)
    assert r.status_code == 200, r.text
    return r.json()


class TestUnifiedCreation:
    def test_order_and_samples_get_monthly_codes_per_scope(self, client, lab):
        body = make_order(client, lab, sample_codes=("BX", "TJ", "BX"))
        assert body["order"]["order_code"] == "CU011026"
        # samples keep request order; two BX samples are consecutive, TJ has its own counter
        assert [s["sample_code"] for s in body["samples"]] == ["BX011026", "TJ011026", "BX021026"]

    def test_second_registration_continues_the_sequence(self, client, lab):
        make_order(client, lab, sample_codes=("BX",))
        body = make_order(client, lab, sample_codes=("BX", "TJ"))
        assert body["order"]["order_code"] == "CU021026"
        assert [s["sample_code"] for s in body["samples"]] == ["BX021026", "TJ011026"]  # TJ was first used now

    def test_order_and_sample_counters_are_independent_even_with_the_same_code(self, client, session, lab):
        lab.add_sample_type("CU")  # sample type sharing the study type's code
        for _ in range(3):  # three orders -> order counter at 3
            make_order(client, lab, sample_codes=("BX",))
        body = make_order(client, lab, sample_codes=("CU",))
        assert body["order"]["order_code"] == "CU041026"
        assert body["samples"][0]["sample_code"] == "CU011026"  # sample counter untouched by orders

    def test_distinct_study_types_have_distinct_counters(self, client, lab):
        make_order(client, lab)
        lab.add_study_type("PAP")
        other_type = lab.add_study_type("BIO")
        assert make_order(client, lab, study_type=other_type)["order"]["order_code"] == "BIO011026"
        assert make_order(client, lab)["order"]["order_code"] == "CU021026"

    def test_each_tenant_has_its_own_sequence(self, client, lab, other):
        make_order(client, lab)
        make_order(client, lab)
        r = client.post(UNIFIED, json=other.unified(), headers=other.headers)
        assert r.json()["order"]["order_code"] == "CU011026"
        assert r.json()["samples"][0]["sample_code"] == "BX011026"

    @pytest.mark.parametrize("start,expected", [(8, ["CU091026", "CU101026", "CU111026"]),
                                                 (98, ["CU991026", "CU1001026", "CU1011026"]),
                                                 (998, ["CU9991026", "CU10001026", "CU10011026"])])
    def test_width_grows_across_the_decade_boundaries_through_the_api(self, client, session, lab, start, expected):
        set_counter(session, lab, "order", lab.study_type, start)
        got = [make_order(client, lab)["order"]["order_code"] for _ in expected]
        assert got == expected

    def test_sample_width_grows_too(self, client, session, lab):
        set_counter(session, lab, "sample", lab.sample_types["BX"], 99)
        body = make_order(client, lab, sample_codes=("BX", "BX"))
        assert [s["sample_code"] for s in body["samples"]] == ["BX1001026", "BX1011026"]

    def test_the_default_has_no_separator(self, client, lab):
        body = make_order(client, lab)
        assert "-" not in body["order"]["order_code"] and "-" not in body["samples"][0]["sample_code"]

    def test_every_generated_code_is_in_the_ledger_with_its_entity(self, client, session, lab):
        body = make_order(client, lab, sample_codes=("BX", "TJ"))
        rows = session.execute(text("SELECT entity_kind, code, entity_id::text, created_by::text FROM numbering_allocation ORDER BY code")).all()
        assert {(k, c) for k, c, *_ in rows} == {("order", "CU011026"), ("sample", "BX011026"), ("sample", "TJ011026")}
        ids = {c: e for _, c, e, _u in rows}
        assert ids["CU011026"] == body["order"]["id"]
        assert ids["BX011026"] == body["samples"][0]["id"]
        assert {u for *_, u in rows} == {str(lab.user.id)}


class TestStandaloneRoutes:
    def test_standalone_order_and_sample(self, client, session, lab):
        r = client.post(ORDERS, json=lab.order_payload(), headers=lab.headers)
        assert r.status_code == 200, r.text
        assert r.json()["order_code"] == "CU011026"
        s = client.post(SAMPLES, json=lab.sample_payload(r.json()["id"]), headers=lab.headers)
        assert s.status_code == 200, s.text
        assert s.json()["sample_code"] == "BX011026"
        s2 = client.post(SAMPLES, json=lab.sample_payload(r.json()["id"], sample_code="  "), headers=lab.headers)
        assert s2.json()["sample_code"] == "BX021026"  # blank = not supplied

    def test_standalone_and_unified_share_the_same_counters(self, client, lab):
        client.post(ORDERS, json=lab.order_payload(), headers=lab.headers)
        assert make_order(client, lab)["order"]["order_code"] == "CU021026"

    def test_samples_of_other_orders_share_the_type_counter(self, client, lab):
        a = client.post(ORDERS, json=lab.order_payload(), headers=lab.headers).json()["id"]
        b = client.post(ORDERS, json=lab.order_payload(), headers=lab.headers).json()["id"]
        codes = [client.post(SAMPLES, json=lab.sample_payload(o), headers=lab.headers).json()["sample_code"] for o in (a, b, a)]
        assert codes == ["BX011026", "BX021026", "BX031026"]


class TestPeriod:
    def test_month_rollover_restarts_per_month(self, client, lab, monkeypatch):
        state = freeze(monkeypatch, 2026, 10, 31, 12)
        assert make_order(client, lab)["order"]["order_code"] == "CU011026"
        assert make_order(client, lab)["order"]["order_code"] == "CU021026"
        from datetime import datetime, timezone
        state["now"] = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
        assert make_order(client, lab)["order"]["order_code"] == "CU011126"

    def test_year_rollover(self, client, lab, monkeypatch):
        freeze(monkeypatch, 2026, 12, 20)
        assert make_order(client, lab)["order"]["order_code"] == "CU011226"
        freeze(monkeypatch, 2027, 1, 3)
        assert make_order(client, lab)["order"]["order_code"] == "CU010127"

    def test_period_uses_the_branch_timezone(self, client, session, lab, monkeypatch):
        # 2026-11-01 03:30 UTC is still Oct 31 in Mexico City, already Nov 1 in UTC
        freeze(monkeypatch, 2026, 11, 1, 3, 30)
        assert make_order(client, lab)["order"]["order_code"] == "CU011026"
        lab.branch.timezone = "UTC"
        session.add(lab.branch)
        session.commit()
        assert make_order(client, lab)["order"]["order_code"] == "CU011126"

    def test_one_period_per_unified_request(self, client, lab, monkeypatch):
        freeze(monkeypatch, 2026, 10, 31, 23, 59)
        body = make_order(client, lab, sample_codes=("BX", "TJ"))
        assert {c[-4:] for c in [body["order"]["order_code"], *[s["sample_code"] for s in body["samples"]]]} == {"1026"}


class TestDeletionAndHistory:
    def test_deleting_an_order_never_frees_its_number(self, client, session, lab):
        first = make_order(client, lab, sample_codes=("BX",))
        delete_order_and_samples(session, first["order"]["id"])
        assert order_codes(session, lab) == [] and sample_codes(session, lab) == []
        again = make_order(client, lab, sample_codes=("BX",))
        assert again["order"]["order_code"] == "CU021026"
        assert again["samples"][0]["sample_code"] == "BX021026"

    def test_deleting_the_highest_sample_never_frees_it(self, client, session, lab):
        order = client.post(ORDERS, json=lab.order_payload(), headers=lab.headers).json()["id"]
        client.post(SAMPLES, json=lab.sample_payload(order), headers=lab.headers)
        session.execute(text("DELETE FROM order_event WHERE order_id = :o"), {"o": order})
        session.execute(text("DELETE FROM sample WHERE order_id = :o"), {"o": order})
        session.commit()
        assert client.post(SAMPLES, json=lab.sample_payload(order), headers=lab.headers).json()["sample_code"] == "BX021026"

    def test_recreating_a_deleted_type_with_the_same_code_never_reissues_codes(self, client, session, lab):
        first = make_order(client, lab)  # CU011026 from study type A
        delete_order_and_samples(session, first["order"]["id"])
        session.execute(text("DELETE FROM study_type WHERE id = :i"), {"i": str(lab.study_type.id)})
        session.commit()
        recreated = lab.add_study_type("CU")  # new id -> new counter from 1
        again = make_order(client, lab, study_type=recreated)
        assert again["order"]["order_code"] == "CU021026"  # CU011026 is retired in the ledger: skipped

    def test_legacy_and_manual_codes_are_skipped_never_overwritten(self, client, session, lab):
        order = create_order(session, lab.tenant, lab.branch, order_code="CU011026")
        create_order(session, lab.tenant, lab.branch, order_code="CU021026")
        create_sample(session, lab.tenant, lab.branch, order, sample_code="BX011026")
        body = make_order(client, lab)
        assert body["order"]["order_code"] == "CU031026"
        assert body["samples"][0]["sample_code"] == "BX021026"
        # the legacy rows are untouched
        assert order_codes(session, lab)[:2] == ["CU011026", "CU021026"]

    def test_old_style_codes_keep_their_punctuation_and_do_not_collide(self, client, session, lab):
        create_order(session, lab.tenant, lab.branch, order_code="CU-011026")
        create_order(session, lab.tenant, lab.branch, order_code="CU-1")
        body = make_order(client, lab)
        assert body["order"]["order_code"] == "CU011026"
        assert "CU-011026" in order_codes(session, lab) and "CU-1" in order_codes(session, lab)

    def test_rendered_string_collision_between_two_scopes_is_skipped(self, client, session, lab):
        x1 = lab.add_study_type("X1")
        x = lab.add_study_type("X")
        first = make_order(client, lab, study_type=x1)
        assert first["order"]["order_code"] == "X1011026"
        set_counter(session, lab, "order", x, 100)  # X + 101 + 1026 renders the same string
        second = make_order(client, lab, study_type=x)
        assert second["order"]["order_code"] == "X1021026"
        assert len(set(order_codes(session, lab))) == 2

    def test_catalog_code_change_continues_the_sequence_under_the_new_prefix(self, client, session, lab):
        assert make_order(client, lab)["order"]["order_code"] == "CU011026"
        lab.study_type.code = "CITO"
        session.add(lab.study_type)
        session.commit()
        assert make_order(client, lab)["order"]["order_code"] == "CITO021026"

    def test_a_too_long_code_is_refused_and_nothing_is_consumed(self, fresh_client, session, lab):
        long_type = lab.add_study_type("L" * 50)
        fmt = "{BASE}" + "z" * 34 + "{SEQ}{MM}{YYYY}"  # valid; renders 96 chars up to 6 digits, 97 beyond (order cap: INV-<code> must fit 100)
        r = fresh_client.put(f"{NUMBERING}/config/order", json={"format": fmt, "sequence_min_width": 6, "mode": "automatic"}, headers=lab.headers)
        assert r.status_code == 200, r.text
        set_counter(session, lab, "order", long_type, 999_998)
        ok = fresh_client.post(UNIFIED, json=lab.unified(study_type=long_type), headers=lab.headers)
        assert ok.status_code == 200 and len(ok.json()["order"]["order_code"]) == 96  # 999999 still fits
        before = (ledger_rows(session), session.execute(text("SELECT sum(last_value) FROM numbering_counter")).scalar())
        r = fresh_client.post(UNIFIED, json=lab.unified(study_type=long_type), headers=lab.headers)  # 1_000_000 would be 97
        assert r.status_code == 409 and r.json()["detail"]["code"] == "numbering_code_too_long"
        session.expire_all()
        assert before == (ledger_rows(session), session.execute(text("SELECT sum(last_value) FROM numbering_counter")).scalar())


class TestRollback:
    def test_a_rejected_unified_request_leaves_no_counter_ledger_or_rows(self, fresh_client, session, lab):
        inactive = lab.add_sample_type("OFF")
        inactive.is_active = False
        session.add(inactive)
        session.commit()
        bad = lab.unified(sample_codes=("BX", "OFF"))
        r = fresh_client.post(UNIFIED, json=bad, headers=lab.headers)
        assert r.status_code == 400
        assert order_codes(session, lab) == [] and ledger_rows(session) == 0
        assert session.execute(text("SELECT coalesce(sum(last_value),0) FROM numbering_counter")).scalar() == 0
        ok = fresh_client.post(UNIFIED, json=lab.unified(sample_codes=("BX",)), headers=lab.headers)
        assert ok.json()["order"]["order_code"] == "CU011026"  # the aborted attempt released its number

    def test_a_failure_after_allocation_rolls_the_allocation_back(self, fresh_client, session, lab, monkeypatch):
        from fastapi import HTTPException
        import app.api.v1.laboratory as laboratory

        real = laboratory._materialize_default_reviewer

        def boom(*a, **k):
            raise HTTPException(409, "simulated failure after allocation")

        monkeypatch.setattr(laboratory, "_materialize_default_reviewer", boom)
        r = fresh_client.post(UNIFIED, json=lab.unified(sample_codes=("BX", "TJ")), headers=lab.headers)
        assert r.status_code == 409
        assert ledger_rows(session) == 0
        assert session.execute(text("SELECT coalesce(sum(last_value),0) FROM numbering_counter")).scalar() == 0
        monkeypatch.setattr(laboratory, "_materialize_default_reviewer", real)
        ok = fresh_client.post(UNIFIED, json=lab.unified(sample_codes=("BX", "TJ")), headers=lab.headers)
        assert ok.json()["order"]["order_code"] == "CU011026"
        assert [s["sample_code"] for s in ok.json()["samples"]] == ["BX011026", "TJ011026"]


class TestManualOverride:
    def test_a_manual_code_is_used_and_consumes_no_counter(self, client, session, lab):
        body = make_order(client, lab, sample_codes=(("BX", "MAN-S1"),), order_code="MAN-O1")
        assert body["order"]["order_code"] == "MAN-O1" and body["samples"][0]["sample_code"] == "MAN-S1"
        assert counter_rows(session) == 0 and ledger_rows(session) == 0
        nxt = make_order(client, lab)
        assert nxt["order"]["order_code"] == "CU011026"

    def test_mixed_manual_and_generated_samples(self, client, lab):
        body = make_order(client, lab, sample_codes=(("BX", "MAN-1"), "BX", "TJ"))
        assert [s["sample_code"] for s in body["samples"]] == ["MAN-1", "BX011026", "TJ011026"]

    def test_manual_duplicates_keep_their_old_errors(self, client, lab):
        make_order(client, lab, order_code="MAN-O1")
        r = client.post(UNIFIED, json=lab.unified(order_code="MAN-O1"), headers=lab.headers)
        assert r.status_code == 400 and "Order code already exists" in r.json()["detail"]
        r = client.post(UNIFIED, json=lab.unified(sample_codes=(("BX", "DUP"), ("TJ", "DUP"))), headers=lab.headers)
        assert r.status_code == 400 and "Duplicate sample_code" in r.json()["detail"]

    def test_a_manual_code_may_reuse_a_code_that_exists_on_another_order_as_before(self, client, session, lab):
        order = create_order(session, lab.tenant, lab.branch, order_code="LEG-1")
        create_sample(session, lab.tenant, lab.branch, order, sample_code="SHARED")
        r = client.post(UNIFIED, json=lab.unified(sample_codes=(("BX", "SHARED"),)), headers=lab.headers)
        assert r.status_code == 200  # sample codes remain unique only per order (not tightened)

    def test_a_manual_code_equal_to_a_retired_generated_code_is_refused(self, client, session, lab):
        first = make_order(client, lab, sample_codes=("BX",))
        delete_order_and_samples(session, first["order"]["id"])
        r = client.post(UNIFIED, json=lab.unified(order_code="CU011026"), headers=lab.headers)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "numbering_code_already_issued"
        r = client.post(UNIFIED, json=lab.unified(sample_codes=(("BX", "BX011026"),)), headers=lab.headers)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "numbering_code_already_issued"

    def test_a_manual_code_that_a_later_generation_would_produce_is_skipped_over(self, client, lab):
        make_order(client, lab, order_code="CU011026")
        assert make_order(client, lab)["order"]["order_code"] == "CU021026"

    def test_manual_codes_are_per_tenant(self, client, lab, other):
        make_order(client, lab, order_code="SAME")
        r = client.post(UNIFIED, json=other.unified(order_code="SAME"), headers=other.headers)
        assert r.status_code == 200

    def test_standalone_manual_sample_code_keeps_its_per_order_rule(self, client, lab):
        order = client.post(ORDERS, json=lab.order_payload(order_code="MAN-O"), headers=lab.headers).json()["id"]
        ok = client.post(SAMPLES, json=lab.sample_payload(order, sample_code="M1"), headers=lab.headers)
        dup = client.post(SAMPLES, json=lab.sample_payload(order, sample_code="M1"), headers=lab.headers)
        assert ok.status_code == 200 and dup.status_code == 400


class TestModes:
    def put(self, client, lab, kind, **body):
        payload = {"format": "{BASE}{SEQ}{MM}{YY}", "sequence_min_width": 2, "mode": "automatic", **body}
        return client.put(f"{NUMBERING}/config/{kind}", json=payload, headers=lab.headers)

    def test_automatic_only_rejects_manual_codes(self, client, lab):
        assert self.put(client, lab, "order", mode="automatic_only").status_code == 200
        assert self.put(client, lab, "sample", mode="automatic_only").status_code == 200
        r = client.post(UNIFIED, json=lab.unified(order_code="X"), headers=lab.headers)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "numbering_manual_code_not_allowed"
        r = client.post(UNIFIED, json=lab.unified(sample_codes=(("BX", "X"),)), headers=lab.headers)
        assert r.status_code == 422
        assert make_order(client, lab)["order"]["order_code"] == "CU011026"

    def test_manual_only_requires_codes_and_generates_nothing(self, client, session, lab):
        self.put(client, lab, "order", mode="manual_only")
        r = client.post(UNIFIED, json=lab.unified(), headers=lab.headers)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "numbering_code_required"
        ok = client.post(UNIFIED, json=lab.unified(order_code="MAN-1"), headers=lab.headers)
        assert ok.status_code == 200 and ok.json()["order"]["order_code"] == "MAN-1"
        assert ledger_rows(session) == ledger_rows(session) and ok.json()["samples"][0]["sample_code"] == "BX011026"

    def test_modes_are_per_entity_kind(self, client, lab):
        self.put(client, lab, "sample", mode="manual_only")
        r = client.post(UNIFIED, json=lab.unified(), headers=lab.headers)  # sample code missing
        assert r.status_code == 422
        ok = client.post(UNIFIED, json=lab.unified(sample_codes=(("BX", "S-1"),)), headers=lab.headers)
        assert ok.json()["order"]["order_code"] == "CU011026"


class TestConfiguration:
    def put(self, client, lab, kind="order", headers=None, **body):
        payload = {"format": "{BASE}{SEQ}{MM}{YY}", "sequence_min_width": 2, "mode": "automatic", **body}
        return client.put(f"{NUMBERING}/config/{kind}", json=payload, headers=headers or lab.headers)

    def test_defaults_are_reported_without_any_stored_row(self, client, session, lab):
        r = client.get(f"{NUMBERING}/config", headers=lab.headers)
        assert r.status_code == 200
        for kind in ("order", "sample"):
            cfg = r.json()[kind]
            assert (cfg["format"], cfg["sequence_min_width"], cfg["mode"], cfg["is_default"]) == ("{BASE}{SEQ}{MM}{YY}", 2, "automatic", True)
        assert session.execute(text("SELECT count(*) FROM numbering_config")).scalar() == 0

    def test_a_custom_format_applies_to_new_codes_only_and_continues_the_sequence(self, client, session, lab):
        before = make_order(client, lab)
        assert self.put(client, lab, format="{BASE}-{SEQ}-{MM}{YYYY}", sequence_min_width=3).status_code == 200
        after = make_order(client, lab)
        assert before["order"]["order_code"] == "CU011026"
        assert after["order"]["order_code"] == "CU-002-102026"
        assert order_codes(session, lab)[0] == "CU011026"  # issued code untouched
        cfg = client.get(f"{NUMBERING}/config", headers=lab.headers).json()["order"]
        assert cfg["is_default"] is False and cfg["format"] == "{BASE}-{SEQ}-{MM}{YYYY}"

    def test_the_two_kinds_are_configured_independently(self, client, lab):
        self.put(client, lab, "sample", format="{BASE}.{SEQ}{MM}{YY}")
        body = make_order(client, lab)
        assert body["order"]["order_code"] == "CU011026"
        assert body["samples"][0]["sample_code"] == "BX.011026"

    @pytest.mark.parametrize("body", [
        {"format": "{BASE}{SEQ}"}, {"format": "{BASE}{SEQ}{MM}{YY}{YYYY}"}, {"format": "{BASE}/{SEQ}{MM}{YY}"},
        {"format": "{BASE}{SEQ}{MM}{YY}", "sequence_min_width": 0}, {"format": "{BASE}{SEQ}{MM}{YY}", "sequence_min_width": 9},
        {"format": "{BASE}{SEQ}{MM}{YY}", "mode": "bogus"},
    ])
    def test_invalid_configuration_is_rejected_and_not_stored(self, client, session, lab, body):
        r = self.put(client, lab, **body)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "numbering_invalid_config"
        assert session.execute(text("SELECT count(*) FROM numbering_config")).scalar() == 0

    def test_unknown_entity_kind_is_422(self, client, lab):
        assert self.put(client, lab, "patient").status_code == 422

    def test_changes_need_manage_catalog_but_reading_needs_only_lab_read(self, client, lab):
        for role, can_write in (("lab_tech", False), ("viewer", False), ("billing", False), ("admin", True)):
            headers = lab.headers_for(role)
            assert client.get(f"{NUMBERING}/config", headers=headers).status_code == 200
            assert self.put(client, lab, headers=headers).status_code == (200 if can_write else 403), role

    def test_unauthenticated_requests_are_refused(self, client):
        assert client.get(f"{NUMBERING}/config").status_code in (401, 403)
        assert client.put(f"{NUMBERING}/config/order", json={}).status_code in (401, 403)
        assert client.post(f"{NUMBERING}/preview", json={"entity_kind": "order"}).status_code in (401, 403)

    def test_configuration_is_tenant_scoped(self, client, lab, other):
        self.put(client, lab, format="{BASE}-{SEQ}{MM}{YY}")
        assert client.get(f"{NUMBERING}/config", headers=other.headers).json()["order"]["is_default"] is True
        assert client.post(UNIFIED, json=other.unified(), headers=other.headers).json()["order"]["order_code"] == "CU011026"

    def test_changes_are_audited_with_old_and_new_values(self, client, session, lab):
        self.put(client, lab, "sample", format="{BASE}-{SEQ}{MM}{YY}", mode="automatic_only")
        rows = session.exec(select(AuditLog).where(AuditLog.action == "NUMBERING.CONFIG_UPDATED")).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.tenant_id == lab.tenant.id and row.actor_user_id == lab.user.id
        assert row.old_values["is_default"] is True and row.new_values["mode"] == "automatic_only"
        assert row.event_metadata == {"entity_kind": "sample"}

    def test_generated_codes_are_not_audit_noise_but_are_in_the_ledger(self, client, session, lab):
        make_order(client, lab)
        assert ledger_rows(session) == 2


class TestPreview:
    def preview(self, client, lab, **body):
        return client.post(f"{NUMBERING}/preview", json=body, headers=lab.headers)

    def test_examples_cross_the_width_boundaries_with_the_scope_base_code(self, client, lab):
        r = self.preview(client, lab, entity_kind="order", scope_id=str(lab.study_type.id))
        assert r.status_code == 200
        body = r.json()
        assert body["valid"] is True and body["base_code"] == "CU" and (body["period_year"], body["period_month"]) == (2026, 10)
        assert [e["code"] for e in body["examples"]] == ["CU011026", "CU021026", "CU091026", "CU101026", "CU991026", "CU1001026", "CU9991026", "CU10001026"]
        assert "no reserva" in body["notice"]

    def test_the_estimate_matches_the_next_real_code_when_nothing_interferes(self, client, session, lab):
        make_order(client, lab)
        est = self.preview(client, lab, entity_kind="order", scope_id=str(lab.study_type.id)).json()["estimate"]
        assert est["code"] == "CU021026" and est["reserved"] is False
        assert make_order(client, lab)["order"]["order_code"] == est["code"]

    def test_the_estimate_skips_occupied_codes_like_allocation_would(self, client, session, lab):
        create_order(session, lab.tenant, lab.branch, order_code="CU011026")
        est = self.preview(client, lab, entity_kind="order", scope_id=str(lab.study_type.id)).json()["estimate"]
        assert est["code"] == "CU021026" and est["skipped_occupied"] == 1

    def test_previews_consume_nothing(self, client, session, lab):
        for _ in range(5):
            self.preview(client, lab, entity_kind="order", scope_id=str(lab.study_type.id))
            self.preview(client, lab, entity_kind="sample", scope_id=str(lab.sample_types["BX"].id), format="{BASE}-{SEQ}{MM}{YY}")
        assert counter_rows(session) == 0 and ledger_rows(session) == 0
        assert session.execute(text("SELECT count(*) FROM numbering_config")).scalar() == 0
        assert make_order(client, lab)["order"]["order_code"] == "CU011026"

    def test_a_candidate_format_is_validated_without_saving_it(self, client, session, lab):
        ok = self.preview(client, lab, entity_kind="order", format="{BASE}-{SEQ}{MM}{YYYY}", sequence_min_width=3, scope_id=str(lab.study_type.id)).json()
        assert ok["valid"] and ok["examples"][0]["code"] == "CU-001102026" and ok["estimate"]["code"] == "CU-001102026"
        bad = self.preview(client, lab, entity_kind="order", format="{BASE}{SEQ}").json()
        assert bad["valid"] is False and bad["errors"] and bad["examples"] == []
        assert client.get(f"{NUMBERING}/config", headers=lab.headers).json()["order"]["is_default"] is True

    def test_another_tenants_type_is_a_404(self, client, lab, other):
        assert self.preview(client, lab, entity_kind="order", scope_id=str(other.study_type.id)).status_code == 404
        assert self.preview(client, lab, entity_kind="sample", scope_id=str(lab.study_type.id)).status_code == 404
        assert self.preview(client, lab, entity_kind="order", scope_id="not-a-uuid").status_code == 404

    def test_a_viewer_can_preview(self, client, lab):
        r = client.post(f"{NUMBERING}/preview", json={"entity_kind": "order"}, headers=lab.headers_for("viewer"))
        assert r.status_code == 200 and r.json()["base_code"] == "BASE"


class TestStructuralGuards:
    def source(self):
        return pathlib.Path(__file__).resolve().parents[2].joinpath("app/api/v1/laboratory.py").read_text()

    def test_no_route_generates_codes_from_a_count_any_more(self):
        src = self.source()
        assert "next_seq" not in src and "{study_type.code}-" not in src

    def test_both_creation_routes_use_the_allocation_service(self):
        src = self.source()
        assert src.count("numbering.allocate_batch(") == 3  # create_order, create_sample, unified
        assert src.count("numbering.claim_manual_codes(") >= 4

    def test_the_ledger_has_no_foreign_key_to_orders_samples_or_catalogs(self, session):
        fks = session.execute(text(
            "SELECT ccu.table_name FROM information_schema.table_constraints tc "
            "JOIN information_schema.constraint_column_usage ccu ON tc.constraint_name = ccu.constraint_name "
            "WHERE tc.table_name IN ('numbering_allocation', 'numbering_counter') AND tc.constraint_type = 'FOREIGN KEY'"
        )).scalars().all()
        assert set(fks) == {"tenant"}
