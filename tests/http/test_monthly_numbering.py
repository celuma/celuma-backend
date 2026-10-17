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
    NUMBERING, ORDERS, SAMPLES, UNIFIED, Lab, configure, counter_rows, delete_order_and_samples, freeze, ledger_rows,
    order_codes, run_parallel, sample_codes, set_counter, grant_manual_permission,
)


@pytest.fixture
def lab(session, monkeypatch):
    freeze(monkeypatch)  # 2026-10-15 18:00 UTC = Oct 15, 12:00 Mexico City
    lab = Lab(session, "Alfa", sample_types=("BX", "TJ"))
    configure(session, lab)  # activation is explicit: a tenant with no stored configuration is NOT activated
    return lab


@pytest.fixture
def other(session):
    lab = Lab(session, "Beta", sample_types=("BX",))
    configure(session, lab)
    return lab


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
        s2 = client.post(SAMPLES, json=lab.sample_payload(r.json()["id"]), headers=lab.headers)
        assert s2.json()["sample_code"] == "BX021026"

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

    def test_one_period_per_unified_request(self, client, lab, monkeypatch):
        freeze(monkeypatch, 2026, 10, 31, 23, 59)
        body = make_order(client, lab, sample_codes=("BX", "TJ"))
        assert {c[-4:] for c in [body["order"]["order_code"], *[s["sample_code"] for s in body["samples"]]]} == {"1026"}


class TestNumberingTimezone:
    """Tenant-wide counters use ONE configurable laboratory zone, never whichever branch handled the request."""

    @pytest.fixture
    def two_branches(self, session, lab):
        from app.models.tenant import Branch
        norte = Branch(tenant_id=lab.tenant.id, code="NORTE", name="Norte", timezone="Pacific/Auckland")
        lab.branch.timezone = "America/Mexico_City"
        session.add(norte)
        session.add(lab.branch)
        session.commit()
        session.refresh(norte)
        return norte

    def order_in(self, client, lab, branch):
        payload = lab.unified()
        payload["branch_id"] = str(branch.id)
        r = client.post(UNIFIED, json=payload, headers=lab.headers)
        assert r.status_code == 200, r.text
        return r.json()

    def test_the_laboratory_zone_defines_the_period_not_the_branch(self, client, session, lab, monkeypatch):
        # 2026-11-01 03:30 UTC: Oct 31 in Mexico City, Nov 1 in UTC
        freeze(monkeypatch, 2026, 11, 1, 3, 30)
        configure(session, lab, timezone="UTC")
        assert lab.branch.timezone == "America/Mexico_City"  # the branch zone is irrelevant for a tenant-wide counter
        assert make_order(client, lab)["order"]["order_code"] == "CU011126"
        configure(session, lab, timezone="America/Mexico_City")
        assert make_order(client, lab)["order"]["order_code"] == "CU011026"

    def test_two_branches_straddling_a_month_boundary_share_one_period_and_one_counter(self, client, session, lab, two_branches, monkeypatch):
        freeze(monkeypatch, 2026, 10, 31, 12)  # Oct 31 12:00 UTC: Nov 1 01:00 in Auckland
        configure(session, lab, timezone="America/Mexico_City")
        a = self.order_in(client, lab, lab.branch)["order"]["order_code"]
        b = self.order_in(client, lab, two_branches)["order"]["order_code"]
        assert (a, b) == ("CU011026", "CU021026")  # Auckland is already in November; the laboratory is not

    def test_a_branch_scoped_counter_uses_its_branchs_own_zone_by_documented_policy(self, client, session, lab, two_branches, monkeypatch):
        freeze(monkeypatch, 2026, 10, 31, 12)
        configure(session, lab, scope="branch", format="{BASE}{BRANCH}{SEQ}{MM}{YY}", timezone="America/Mexico_City")
        mx = self.order_in(client, lab, lab.branch)["order"]["order_code"]
        nz = self.order_in(client, lab, two_branches)["order"]["order_code"]
        assert (mx, nz) == ("CUMAIN011026", "CUNORTE011126")  # same instant, each branch's own local month

    def test_each_entity_kind_has_its_own_zone_and_one_unified_request_uses_one_instant(self, client, session, lab, monkeypatch):
        freeze(monkeypatch, 2026, 11, 1, 3, 30)
        configure(session, lab, "order", timezone="UTC")
        configure(session, lab, "sample", timezone="America/Mexico_City")
        body = make_order(client, lab)
        assert body["order"]["order_code"] == "CU011126"          # the order kind's zone: UTC -> November
        assert body["samples"][0]["sample_code"] == "BX011026"    # the sample kind's zone: Mexico City -> October

    @pytest.mark.parametrize("bad", ["", "Mars/Olympus", "../etc/passwd", "UTC\n", "América/México", " UTC", "utc;drop", "A" * 101, 5, None])
    def test_unknown_or_malformed_zones_are_rejected_not_guessed(self, client, session, lab, bad):
        r = put_config(client, lab.headers, timezone=bad)
        assert r.status_code == 422
        pr = client.post(f"{NUMBERING}/preview", json={"entity_kind": "order", "timezone": bad}, headers=lab.headers)
        if isinstance(bad, str):
            assert pr.status_code == 200 and pr.json()["valid"] is False
        assert session.execute(text("SELECT timezone FROM numbering_config WHERE entity_kind = 'order'")).scalar() == "America/Mexico_City"

    def test_changing_the_zone_never_resets_counters_or_reissues_a_code(self, client, session, lab, monkeypatch):
        freeze(monkeypatch, 2026, 11, 1, 3, 30)  # Oct 31 in Mexico City, Nov 1 in UTC
        codes = [make_order(client, lab)["order"]["order_code"]]                    # Mexico: CU011026
        put_config(client, lab.headers, timezone="UTC")
        codes.append(make_order(client, lab)["order"]["order_code"])                # UTC: CU011126
        put_config(client, lab.headers, timezone="America/Mexico_City")
        codes.append(make_order(client, lab)["order"]["order_code"])                # back: October counter RESUMES
        assert codes == ["CU011026", "CU011126", "CU021026"]
        assert len(set(order_codes(session, lab))) == 3
        assert session.execute(text("SELECT count(*) FROM numbering_counter WHERE entity_kind = 'order'")).scalar() == 2

    def test_the_zone_is_configuration_audited_and_shown(self, client, session, lab):
        r = put_config(client, lab.headers, "sample", timezone="Europe/Madrid")
        assert r.status_code == 200 and r.json()["timezone"] == "Europe/Madrid"
        assert client.get(f"{NUMBERING}/config", headers=lab.headers).json()["sample"]["timezone"] == "Europe/Madrid"
        row = session.exec(select(AuditLog).where(AuditLog.action == "NUMBERING.CONFIG_UPDATED")).all()[-1]
        assert row.old_values["timezone"] == "America/Mexico_City" and row.new_values["timezone"] == "Europe/Madrid"

    def test_the_preview_uses_the_candidate_zone_and_reports_it(self, client, lab, monkeypatch):
        freeze(monkeypatch, 2026, 11, 1, 3, 30)
        utc = client.post(f"{NUMBERING}/preview", json={"entity_kind": "order", "timezone": "UTC", "scope_id": str(lab.study_type.id)}, headers=lab.headers).json()
        mx = client.post(f"{NUMBERING}/preview", json={"entity_kind": "order", "scope_id": str(lab.study_type.id)}, headers=lab.headers).json()
        assert (utc["timezone"], utc["period_month"], utc["estimate"]["code"]) == ("UTC", 11, "CU011126")
        assert (mx["timezone"], mx["period_month"], mx["estimate"]["code"]) == ("America/Mexico_City", 10, "CU011026")


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
        configure(session, lab, "order", format=fmt, sequence_min_width=6)
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


def put_config(client, headers, kind="order", **body):
    payload = {"active": True, "manual_policy": "allowed", "format": "{BASE}{SEQ}{MM}{YY}", "sequence_min_width": 2,
               "start_value": 1, "scope": "tenant", **body}
    return client.put(f"{NUMBERING}/config/{kind}", json=payload, headers=headers)


class TestRegistrationIsAutomatic:
    """Céluma 1.4 (owner, 2026-10-16): no registration route accepts a typed order or sample code."""

    @pytest.mark.parametrize("route", ["order", "sample", "unified_order", "unified_sample"])
    def test_a_typed_code_is_refused_explicitly_and_nothing_is_created_or_consumed(self, client, session, lab, route):
        existing = client.post(ORDERS, json=lab.order_payload(), headers=lab.headers).json()["id"]
        counters, ledger = counter_rows(session), ledger_rows(session)
        orders = order_codes(session, lab)
        if route == "order":
            res = client.post(ORDERS, json=lab.order_payload(order_code="MAN-1"), headers=lab.headers)
        elif route == "sample":
            res = client.post(SAMPLES, json=lab.sample_payload(existing, sample_code="MAN-S"), headers=lab.headers)
        elif route == "unified_order":
            res = client.post(UNIFIED, json=lab.unified(order_code="MAN-1"), headers=lab.headers)
        else:
            payload = lab.unified()
            payload["samples"][0]["sample_code"] = "MAN-S"
            res = client.post(UNIFIED, json=payload, headers=lab.headers)
        assert res.status_code == 422, res.text
        assert res.json()["detail"][0]["type"] == "numbering_manual_code_not_supported"
        assert "generated automatically" in res.json()["detail"][0]["msg"]
        assert (counter_rows(session), ledger_rows(session), order_codes(session, lab)) == (counters, ledger, orders)

    def test_a_blank_typed_code_is_refused_too_not_silently_ignored(self, client, lab):
        res = client.post(UNIFIED, json=lab.unified(order_code=""), headers=lab.headers)
        assert res.status_code == 422

    def test_no_capability_is_needed_to_register(self, client, session, lab):
        assistant = lab.headers_for("assistant")  # creates orders and samples; does NOT hold lab:enter_manual_code
        ok = client.post(UNIFIED, json=lab.unified(), headers=assistant)
        assert ok.status_code == 200 and ok.json()["order"]["order_code"] == "CU011026"

    def test_the_capability_still_exists_for_admin_and_superuser_only(self, session):
        rows = set(session.execute(text(
            "SELECT r.code FROM role r JOIN role_permission rp ON rp.role_id = r.id "
            "JOIN permission p ON p.id = rp.permission_id WHERE p.code = 'lab:enter_manual_code'")).scalars())
        assert rows == {"admin", "superuser"}  # it now authorizes only the controlled sample-code correction

    @pytest.mark.parametrize("policy", ["allowed", "forbidden", "required"])
    @pytest.mark.parametrize("active", [True, False])
    def test_the_retired_manual_policy_never_blocks_or_changes_registration(self, client, session, lab, policy, active):
        configure(session, lab, manual_policy=policy, active=active)
        body = make_order(client, lab, sample_codes=("BX", "BX", "TJ"))
        codes = [body["order"]["order_code"]] + [s["sample_code"] for s in body["samples"]]
        expected = ["CU011026", "BX011026", "BX021026", "TJ011026"] if active else ["CU-1", "BX-1", "BX-2", "TJ-1"]
        assert codes == expected
        standalone = client.post(SAMPLES, json=lab.sample_payload(body["order"]["id"]), headers=lab.headers)
        assert standalone.status_code == 200
        assert standalone.json()["sample_code"] == ("BX031026" if active else "BX-3")
        # the stored value is never rewritten by registering
        assert session.execute(text("SELECT manual_policy FROM numbering_config WHERE entity_kind = 'order'")).scalar() == policy

    def test_a_laboratory_with_no_configuration_registers_orders_and_samples(self, client, session, monkeypatch):
        freeze(monkeypatch)
        fresh = Lab(session, "SinConfig", sample_types=("BX",))  # no numbering_config row at all
        assert session.execute(text("SELECT count(*) FROM numbering_config")).scalar() == 0
        body = client.post(UNIFIED, json=fresh.unified(("BX", "BX")), headers=fresh.headers).json()
        assert body["order"]["order_code"] == "CU-1"
        assert [s["sample_code"] for s in body["samples"]] == ["BX-1", "BX-2"]
        assert session.execute(text("SELECT count(*) FROM numbering_config")).scalar() == 0  # nothing activated silently

    def test_the_config_endpoint_reports_automatic_registration_and_no_input_hints(self, client, session, lab):
        cfg = client.get(f"{NUMBERING}/config", headers=lab.headers).json()
        for kind in ("order", "sample"):
            assert cfg[kind]["registration"] == "automatic" and cfg[kind]["legacy_manual_policy"] is False
            assert "manual_entry" not in cfg[kind]
        configure(session, lab, "order", manual_policy="required")
        cfg = client.get(f"{NUMBERING}/config", headers=lab.headers).json()
        assert cfg["order"]["legacy_manual_policy"] is True and cfg["order"]["manual_policy"] == "required"

    def test_a_manual_policy_of_required_cannot_be_newly_set_but_a_stored_one_is_preserved(self, client, session, lab):
        res = put_config(client, lab.headers, "order", manual_policy="required")
        assert res.status_code == 422 and res.json()["detail"]["code"] == "numbering_manual_policy_unsupported"
        configure(session, lab, "order", manual_policy="required")
        no_policy = {"active": True, "format": "{BASE}{SEQ}{MM}{YY}", "sequence_min_width": 3, "start_value": 1, "scope": "tenant"}
        res = client.put(f"{NUMBERING}/config/order", json=no_policy, headers=lab.headers)
        assert res.status_code == 200 and res.json()["manual_policy"] == "required"  # omitted = untouched
        assert make_order(client, lab)["order"]["order_code"] == "CU0011026"
        # an administrator may still move the retired value away from `required`
        assert put_config(client, lab.headers, "order", manual_policy="allowed").json()["manual_policy"] == "allowed"
        assert put_config(client, lab.headers, "order", manual_policy="bogus").status_code == 422

    def test_activation_is_still_deliberate_and_per_kind(self, client, session, monkeypatch):
        freeze(monkeypatch)
        legacy = Lab(session, "Legacy2", sample_types=("BX",))
        put_config(client, legacy.headers, "sample")
        body = client.post(UNIFIED, json=legacy.unified(), headers=legacy.headers).json()
        assert body["order"]["order_code"] == "CU-1"  # orders stayed inactive
        assert body["samples"][0]["sample_code"] == "BX011026"  # only samples were activated


class TestActivationAndCompatibility:
    """A tenant is switched deliberately, never by the migration: until activated it keeps the pre-numbering behaviour."""

    @pytest.fixture
    def legacy(self, session, monkeypatch):
        freeze(monkeypatch)
        return Lab(session, "Legacy", sample_types=("BX",))  # NO configure(): never activated

    def test_defaults_report_not_activated_and_store_nothing(self, client, session, legacy):
        cfg = client.get(f"{NUMBERING}/config", headers=legacy.headers).json()
        for kind in ("order", "sample"):
            assert cfg[kind]["active"] is False and cfg[kind]["is_default"] is True
        assert cfg["order"]["registration"] == cfg["sample"]["registration"] == "automatic"
        assert session.execute(text("SELECT count(*) FROM numbering_config")).scalar() == 0

    def test_inactive_orders_keep_the_old_shape_from_a_race_safe_counter(self, client, session, legacy):
        codes = [client.post(ORDERS, json=legacy.order_payload(), headers=legacy.headers).json()["order_code"] for _ in range(3)]
        assert codes == ["CU-1", "CU-2", "CU-3"]
        scheme = session.execute(text("SELECT DISTINCT scheme FROM numbering_allocation")).scalars().all()
        assert scheme == ["legacy"]

    def test_the_compatibility_counter_continues_from_existing_orders_and_never_goes_back_after_a_deletion(self, client, session, legacy):
        for n in (1, 2, 3):
            create_order(session, legacy.tenant, legacy.branch, order_code=f"CU-{n}")
            session.execute(text('UPDATE "order" SET study_type_id = :s WHERE order_code = :c'),
                            {"s": str(legacy.study_type.id), "c": f"CU-{n}"})
        session.commit()
        first = client.post(ORDERS, json=legacy.order_payload(), headers=legacy.headers).json()
        assert first["order_code"] == "CU-4"  # continues where count + 1 would have
        delete_order_and_samples(session, first["id"])
        assert client.post(ORDERS, json=legacy.order_payload(), headers=legacy.headers).json()["order_code"] == "CU-5"

    def test_the_compatibility_counter_skips_existing_codes_instead_of_colliding(self, client, session, legacy):
        create_order(session, legacy.tenant, legacy.branch, order_code="CU-1")  # not linked to the study type
        assert client.post(ORDERS, json=legacy.order_payload(), headers=legacy.headers).json()["order_code"] == "CU-2"

    def test_inactive_samples_are_generated_by_the_compatibility_allocator_too(self, client, session, legacy):
        order = client.post(ORDERS, json=legacy.order_payload(), headers=legacy.headers).json()["id"]
        codes = [client.post(SAMPLES, json=legacy.sample_payload(order), headers=legacy.headers).json()["sample_code"]
                 for _ in range(3)]
        assert codes == ["BX-1", "BX-2", "BX-3"]  # the same type twice in one order is fine
        rows = session.execute(text(
            "SELECT scheme, period_year, period_month FROM numbering_allocation WHERE entity_kind = 'sample'")).all()
        assert set(rows) == {("legacy", 0, 0)}

    def test_the_sample_compatibility_counter_follows_existing_codes_and_never_rewinds(self, client, session, legacy):
        order = create_order(session, legacy.tenant, legacy.branch, order_code="OLD-1")
        for code in ("BX-7", "BX-x", "M1", "BX-50"):
            create_sample(session, legacy.tenant, legacy.branch, order, sample_code=code, sample_type=legacy.sample_types["BX"])
        first = client.post(UNIFIED, json=legacy.unified(("BX",)), headers=legacy.headers).json()
        assert first["samples"][0]["sample_code"] == "BX-51"  # highest valid legacy sequence + 1, irregular shapes ignored
        delete_order_and_samples(session, first["order"]["id"])
        again = client.post(UNIFIED, json=legacy.unified(("BX",)), headers=legacy.headers).json()
        assert again["samples"][0]["sample_code"] == "BX-52"  # a deletion never lowers it
        assert {c for c in sample_codes(session, legacy)} >= {"BX-7", "BX-x", "M1", "BX-50"}  # old codes untouched

    def test_sample_and_order_counters_stay_independent_when_inactive(self, client, session, legacy):
        body = client.post(UNIFIED, json=legacy.unified(("BX", "BX")), headers=legacy.headers).json()
        assert body["order"]["order_code"] == "CU-1"
        assert [s["sample_code"] for s in body["samples"]] == ["BX-1", "BX-2"]
        assert client.post(UNIFIED, json=legacy.unified(("BX",)), headers=legacy.headers).json()["order"]["order_code"] == "CU-2"

    def test_inactive_registration_needs_no_capability(self, client, session, legacy):
        r = client.post(UNIFIED, json=legacy.unified(), headers=legacy.headers_for("assistant"))
        assert r.status_code == 200 and r.json()["order"]["order_code"] == "CU-1"

    def test_activation_is_a_deliberate_audited_act_and_only_for_the_chosen_kind(self, client, session, legacy):
        r = put_config(client, legacy.headers, "order")
        assert r.status_code == 200 and r.json()["active"] is True and r.json()["activated_at"]
        rows = session.exec(select(AuditLog).where(AuditLog.action == "NUMBERING.CONFIG_UPDATED")).all()
        assert len(rows) == 1 and rows[0].event_metadata == {"entity_kind": "order", "activated": True}
        assert rows[0].old_values["active"] is False and rows[0].new_values["active"] is True
        body = client.post(ORDERS, json=legacy.order_payload(), headers=legacy.headers).json()
        assert body["order_code"] == "CU011026"
        cfg = client.get(f"{NUMBERING}/config", headers=legacy.headers).json()
        assert cfg["sample"]["active"] is False  # samples were not activated

    def test_activation_never_renumbers_old_codes_and_old_codes_never_block_new_ones(self, client, session, legacy):
        old = client.post(ORDERS, json=legacy.order_payload(), headers=legacy.headers).json()["order_code"]
        put_config(client, legacy.headers, "order")
        new = client.post(ORDERS, json=legacy.order_payload(), headers=legacy.headers).json()["order_code"]
        assert (old, new) == ("CU-1", "CU011026")
        assert "CU-1" in order_codes(session, legacy)

    def test_deactivating_returns_to_compatibility_without_reissuing_anything(self, client, session, legacy):
        assert client.post(ORDERS, json=legacy.order_payload(), headers=legacy.headers).json()["order_code"] == "CU-1"
        put_config(client, legacy.headers, "order")
        client.post(ORDERS, json=legacy.order_payload(), headers=legacy.headers)  # CU011026
        put_config(client, legacy.headers, "order", active=False)
        assert client.post(ORDERS, json=legacy.order_payload(), headers=legacy.headers).json()["order_code"] == "CU-2"
        put_config(client, legacy.headers, "order")
        assert client.post(ORDERS, json=legacy.order_payload(), headers=legacy.headers).json()["order_code"] == "CU021026"


class TestCompatibilitySeed:
    """The compatibility allocator follows EVIDENCE (the highest legacy sequence and the ledger), never the row count."""

    @pytest.fixture
    def legacy(self, session, monkeypatch):
        freeze(monkeypatch)
        return Lab(session, "Legacy", sample_types=("BX",))

    def add(self, session, lab, *codes):
        for code in codes:
            create_order(session, lab.tenant, lab.branch, order_code=code)

    def next_code(self, client, lab):
        r = client.post(ORDERS, json=lab.order_payload(), headers=lab.headers)
        assert r.status_code == 200, r.text
        return r.json()["order_code"]

    def test_a_sparse_history_follows_the_highest_sequence_not_the_row_count(self, client, session, legacy):
        self.add(session, legacy, "CU-1", "CU-50")  # two rows, high-water 50
        assert self.next_code(client, legacy) == "CU-51"
        assert self.next_code(client, legacy) == "CU-52"

    def test_irregular_and_foreign_shapes_are_ignored_and_never_altered(self, client, session, legacy):
        irregular = ["CU-7x", "CU--3", "CU-007-A", "CU-12345678901", "cu-900", "XCU-800", "CU_700", "CU-", "CU-1.5", "BX-600", "CU 400", "CU-٣"]
        self.add(session, legacy, "CU-4", *irregular)
        assert self.next_code(client, legacy) == "CU-5"
        assert set(irregular) | {"CU-4", "CU-5"} <= set(order_codes(session, legacy))  # all preserved exactly

    def test_zero_padded_legacy_numbers_count_by_value(self, client, session, legacy):
        self.add(session, legacy, "CU-007")
        assert self.next_code(client, legacy) == "CU-8"

    def test_only_this_study_types_prefix_counts_and_like_wildcards_are_literal(self, client, session, legacy):
        odd = legacy.add_study_type("A_B")
        self.add(session, legacy, "AXB-99", "A_B-5", "A%B-77", "CU-3")
        r = client.post(ORDERS, json=legacy.order_payload(study_type=odd), headers=legacy.headers)
        assert r.json()["order_code"] == "A_B-6"  # AXB-99 and A%B-77 are not A_B-<n>
        assert self.next_code(client, legacy) == "CU-4"

    def test_the_count_of_rows_is_irrelevant(self, client, session, legacy):
        self.add(session, legacy, *[f"CU-{n}" for n in (1, 2, 3, 4, 5, 6, 7, 8, 9)], "OTHER-1", "MAN-1")
        assert self.next_code(client, legacy) == "CU-10"
        legacy2 = Lab(session, "Legacy2", sample_types=("BX",))
        self.add(session, legacy2, "CU-1", "CU-200")
        assert self.next_code(client, legacy2) == "CU-201"  # two rows, not CU-3

    def test_the_counter_never_rewinds_after_a_deletion(self, client, session, legacy):
        self.add(session, legacy, "CU-1", "CU-50")
        first = client.post(ORDERS, json=legacy.order_payload(), headers=legacy.headers).json()
        assert first["order_code"] == "CU-51"
        delete_order_and_samples(session, first["id"])
        session.execute(text('DELETE FROM "order" WHERE order_code = \'CU-50\''))
        session.commit()
        assert self.next_code(client, legacy) == "CU-52"  # neither deletion lowers the high-water mark

    def test_the_ledger_remembers_a_deleted_highest_code_even_if_the_counter_row_is_gone(self, client, session, legacy):
        self.add(session, legacy, "CU-3")
        first = client.post(ORDERS, json=legacy.order_payload(), headers=legacy.headers).json()
        assert first["order_code"] == "CU-4"
        delete_order_and_samples(session, first["id"])
        session.execute(text("DELETE FROM numbering_counter"))
        session.commit()
        assert self.next_code(client, legacy) == "CU-5"  # evidence: ledger sequence_value 4

    def test_history_deleted_before_the_ledger_existed_cannot_be_reconstructed(self, client, session, legacy):
        """The documented limit: with CU-50 deleted before any ledger/counter and no trace left,
        the allocator can only follow what remains. It claims nothing about absent history."""
        self.add(session, legacy, "CU-1")
        assert self.next_code(client, legacy) == "CU-2"

    def test_a_legacy_code_that_appears_later_moves_the_counter_forward(self, client, session, legacy):
        assert self.next_code(client, legacy) == "CU-1"
        self.add(session, legacy, "CU-80")  # e.g. imported or restored after the first allocation
        assert self.next_code(client, legacy) == "CU-81"

    def test_an_occupied_code_is_still_skipped_by_the_ledger_and_live_rows(self, client, session, legacy):
        self.add(session, legacy, "CU-1")
        # an order whose code collides with the NEXT number but does not match the evidence rule
        # is impossible (it would match), so occupy it through the ledger instead
        session.execute(text(
            "INSERT INTO numbering_allocation (id, tenant_id, entity_kind, scope_id, branch_id, period_year, period_month, "
            "sequence_value, code, scheme, format, sequence_min_width, created_at) VALUES (gen_random_uuid(), :t, 'order', "
            ":s, '00000000-0000-0000-0000-000000000000', 0, 0, 2, 'CU-2', 'legacy', 'x', 2, now())"),
            {"t": str(legacy.tenant.id), "s": str(legacy.study_type.id)})
        session.commit()
        assert self.next_code(client, legacy) == "CU-3"

    def test_concurrent_first_use_on_a_sparse_history_is_unique_and_contiguous(self, fresh_client, session, legacy):
        self.add(session, legacy, "CU-1", "CU-50")
        results = run_parallel([lambda c: c.post(ORDERS, json=legacy.order_payload(), headers=legacy.headers) for _ in range(8)])
        assert all(not isinstance(r, Exception) and r.status_code == 200 for r in results), [getattr(r, "text", r) for r in results]
        assert sorted(r.json()["order_code"] for r in results) == sorted(f"CU-{n}" for n in range(51, 59))

    def test_the_estimate_follows_the_same_evidence(self, client, session, legacy):
        self.add(session, legacy, "CU-1", "CU-50")
        est = client.post(f"{NUMBERING}/preview", json={"entity_kind": "order", "scope_id": str(legacy.study_type.id)}, headers=legacy.headers).json()
        assert est["scheme"] == "legacy" and est["estimate"]["code"] == "CU-51"
        assert counter_rows(session) == 0 and ledger_rows(session) == 0


class TestStartValue:
    def test_the_first_code_of_a_new_counter_uses_the_start_value(self, client, session, lab):
        configure(session, lab, "order", start_value=500)
        assert make_order(client, lab)["order"]["order_code"] == "CU5001026"
        assert make_order(client, lab)["order"]["order_code"] == "CU5011026"

    def test_each_new_month_starts_again_at_the_start_value(self, client, session, lab, monkeypatch):
        configure(session, lab, "order", start_value=10)
        state = freeze(monkeypatch, 2026, 10, 20)
        assert make_order(client, lab)["order"]["order_code"] == "CU101026"
        from datetime import datetime, timezone
        state["now"] = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
        assert make_order(client, lab)["order"]["order_code"] == "CU101126"

    def test_raising_the_start_value_moves_an_existing_counter_forward(self, client, session, lab):
        make_order(client, lab)
        make_order(client, lab)  # counter at 2
        configure(session, lab, "order", start_value=100)
        assert make_order(client, lab)["order"]["order_code"] == "CU1001026"
        assert make_order(client, lab)["order"]["order_code"] == "CU1011026"

    def test_lowering_the_start_value_never_rewinds_or_reissues(self, client, session, lab):
        configure(session, lab, "order", start_value=50)
        assert make_order(client, lab)["order"]["order_code"] == "CU501026"
        configure(session, lab, "order", start_value=1)
        assert make_order(client, lab)["order"]["order_code"] == "CU511026"
        assert session.execute(text("SELECT last_value FROM numbering_counter WHERE entity_kind = 'order'")).scalar() == 51

    def test_a_start_value_inside_an_occupied_range_skips_existing_codes(self, client, session, lab):
        create_order(session, lab.tenant, lab.branch, order_code="CU071026")
        configure(session, lab, "order", start_value=7)
        assert make_order(client, lab)["order"]["order_code"] == "CU081026"

    @pytest.mark.parametrize("value", [0, -3, 100_000_000, "5", 1.5, True])
    def test_invalid_start_values_are_rejected(self, client, session, lab, value):
        r = put_config(client, lab.headers, start_value=value)
        assert r.status_code == 422  # out of range (numbering_invalid_config) or not an integer (schema)

    def test_the_start_value_applies_independently_per_kind(self, client, session, lab):
        configure(session, lab, "sample", start_value=300)
        body = make_order(client, lab)
        assert body["order"]["order_code"] == "CU011026" and body["samples"][0]["sample_code"] == "BX3001026"


class TestBranchScope:
    @pytest.fixture
    def two_branches(self, session, lab):
        from app.models.tenant import Branch
        second = Branch(tenant_id=lab.tenant.id, code="NORTE", name="Norte")
        lab.branch.code = "MAIN"
        session.add(second)
        session.add(lab.branch)
        session.commit()
        session.refresh(second)
        return second

    def unified_in(self, lab, branch, **kw):
        payload = lab.unified(**kw)
        payload["branch_id"] = str(branch.id)
        return payload

    def test_each_branch_has_its_own_sequence_and_the_branch_is_part_of_the_code(self, client, session, lab, two_branches):
        configure(session, lab, scope="branch", format="{BASE}{BRANCH}{SEQ}{MM}{YY}")
        a = client.post(UNIFIED, json=self.unified_in(lab, lab.branch), headers=lab.headers).json()
        b = client.post(UNIFIED, json=self.unified_in(lab, two_branches), headers=lab.headers).json()
        a2 = client.post(UNIFIED, json=self.unified_in(lab, lab.branch), headers=lab.headers).json()
        assert [a["order"]["order_code"], b["order"]["order_code"], a2["order"]["order_code"]] == [
            "CUMAIN011026", "CUNORTE011026", "CUMAIN021026"]
        assert [s["sample_code"] for s in b["samples"]] == ["BXNORTE011026"]

    def test_the_same_sequence_in_two_branches_never_renders_the_same_string(self, client, session, lab, two_branches):
        configure(session, lab, scope="branch", format="{BASE}-{BRANCH}-{SEQ}{MM}{YY}")
        codes = {client.post(UNIFIED, json=self.unified_in(lab, br), headers=lab.headers).json()["order"]["order_code"]
                 for br in (lab.branch, two_branches, lab.branch, two_branches)}
        assert codes == {"CU-MAIN-011026", "CU-NORTE-011026", "CU-MAIN-021026", "CU-NORTE-021026"}

    def test_a_branch_format_without_the_branch_token_is_rejected_and_the_reverse_too(self, client, lab):
        r = put_config(client, lab.headers, scope="branch")
        assert r.status_code == 422 and "BRANCH" in r.json()["detail"]["message"]
        r = put_config(client, lab.headers, scope="tenant", format="{BASE}{BRANCH}{SEQ}{MM}{YY}")
        assert r.status_code == 422 and "BRANCH" in r.json()["detail"]["message"]

    def test_the_counter_keys_follow_the_scope_and_the_ledger_records_the_branch(self, client, session, lab, two_branches):
        configure(session, lab, scope="branch", format="{BASE}{BRANCH}{SEQ}{MM}{YY}")
        client.post(UNIFIED, json=self.unified_in(lab, two_branches), headers=lab.headers)
        branches = session.execute(text("SELECT DISTINCT branch_id::text FROM numbering_counter WHERE entity_kind = 'order'")).scalars().all()
        assert branches == [str(two_branches.id)]
        configure(session, lab)  # back to tenant scope
        client.post(UNIFIED, json=lab.unified(), headers=lab.headers)
        rows = session.execute(text("SELECT branch_id::text, count(*) FROM numbering_counter WHERE entity_kind = 'order' GROUP BY 1")).all()
        assert {r[0] for r in rows} == {str(two_branches.id), "00000000-0000-0000-0000-000000000000"}

    def test_switching_tenant_to_branch_and_back_never_rewinds_or_reissues(self, client, session, lab, two_branches):
        assert make_order(client, lab)["order"]["order_code"] == "CU011026"
        assert make_order(client, lab)["order"]["order_code"] == "CU021026"
        configure(session, lab, scope="branch", format="{BASE}{BRANCH}{SEQ}{MM}{YY}")
        assert client.post(UNIFIED, json=self.unified_in(lab, lab.branch), headers=lab.headers).json()["order"]["order_code"] == "CUMAIN011026"
        configure(session, lab)  # tenant scope again: the tenant counter resumes where it stopped
        assert make_order(client, lab)["order"]["order_code"] == "CU031026"
        assert len(set(order_codes(session, lab))) == 4

    def test_an_unsafe_branch_code_is_refused_before_anything_is_consumed(self, client, session, lab, two_branches):
        configure(session, lab, scope="branch", format="{BASE}{BRANCH}{SEQ}{MM}{YY}")
        two_branches.code = "NO RTE/1"
        session.add(two_branches)
        session.commit()
        r = client.post(UNIFIED, json=self.unified_in(lab, two_branches), headers=lab.headers)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "numbering_branch_code_unsafe"

    def test_two_branches_with_codes_that_render_the_same_text_are_still_unique(self, client, session, lab, two_branches):
        # `CU` + `1` + `01` and `CU` + `10` + `1`... the ledger keeps the full strings unique
        configure(session, lab, scope="branch", format="{BASE}{BRANCH}{SEQ}{MM}{YY}", sequence_min_width=1)
        two_branches.code = "1"
        lab.branch.code = "10"
        session.add(two_branches)
        session.add(lab.branch)
        session.commit()
        a = client.post(UNIFIED, json=self.unified_in(lab, lab.branch), headers=lab.headers).json()["order"]["order_code"]
        for _ in range(11):
            client.post(UNIFIED, json=self.unified_in(lab, two_branches), headers=lab.headers)
        codes = order_codes(session, lab)
        assert len(codes) == len(set(codes)) and a in codes

    def test_branch_scoped_preview_uses_the_selected_branch_code(self, client, session, lab, two_branches):
        configure(session, lab, scope="branch", format="{BASE}-{BRANCH}-{SEQ}{MM}{YY}")
        r = client.post(f"{NUMBERING}/preview", json={
            "entity_kind": "order", "scope_id": str(lab.study_type.id), "branch_id": str(two_branches.id)}, headers=lab.headers).json()
        assert r["valid"] and r["estimate"]["code"] == "CU-NORTE-011026"
        none = client.post(f"{NUMBERING}/preview", json={"entity_kind": "order", "scope": "branch", "format": "{BASE}-{BRANCH}-{SEQ}{MM}{YY}"}, headers=lab.headers).json()
        assert none["examples"][0]["code"] == "BASE-BRANCH-011026"


class TestSeparators:
    def test_default_is_without_separator_and_the_hyphen_is_an_option(self, client, session, lab):
        assert make_order(client, lab)["order"]["order_code"] == "CU011026"
        assert put_config(client, lab.headers, format="{BASE}-{SEQ}{MM}{YY}").status_code == 200
        assert make_order(client, lab)["order"]["order_code"] == "CU-021026"  # same counter, new shape

    def test_hyphen_codes_for_both_kinds_and_every_width_boundary(self, client, session, lab):
        configure(session, lab, format="{BASE}-{SEQ}{MM}{YY}")
        set_counter(session, lab, "order", lab.study_type, 98)
        got = [make_order(client, lab)["order"]["order_code"] for _ in range(3)]
        assert got == ["CU-991026", "CU-1001026", "CU-1011026"]
        assert make_order(client, lab)["samples"][0]["sample_code"] == "BX-041026"  # three samples already issued

    def test_both_formats_search_and_coexist_with_old_punctuation(self, client, session, lab):
        create_order(session, lab.tenant, lab.branch, order_code="CU-011026")  # legacy-style, same digits
        assert make_order(client, lab)["order"]["order_code"] == "CU011026"  # no collision: different strings
        configure(session, lab, format="{BASE}-{SEQ}{MM}{YY}")
        assert make_order(client, lab)["order"]["order_code"] == "CU-021026"  # CU-011026 is occupied -> next value

    @pytest.mark.parametrize("fmt", [
        "{BASE}\n{SEQ}{MM}{YY}", "{BASE}\t{SEQ}{MM}{YY}", "{BASE}\x00{SEQ}{MM}{YY}", "{BASE}\u202e{SEQ}{MM}{YY}",
        "{BASE}\u200b{SEQ}{MM}{YY}", "{BASE}é{SEQ}{MM}{YY}", "{BASE} {SEQ}{MM}{YY}", "{BASE}/{SEQ}{MM}{YY}",
        "{BASE}\\{SEQ}{MM}{YY}", "{BASE}';--{SEQ}{MM}{YY}", "{BASE}<b>{SEQ}{MM}{YY}", "{BASE}{SEQ}{MM}{YY}\n",
        "{BASE.lower()}{SEQ}{MM}{YY}", "{__import__('os')}{BASE}{SEQ}{MM}{YY}", "${BASE}{SEQ}{MM}{YY}", "%s{BASE}{SEQ}{MM}{YY}",
    ])
    def test_unsafe_literals_and_expressions_are_always_rejected(self, client, session, lab, fmt):
        r = put_config(client, lab.headers, format=fmt)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "numbering_invalid_config"
        preview = client.post(f"{NUMBERING}/preview", json={"entity_kind": "order", "format": fmt}, headers=lab.headers).json()
        assert preview["valid"] is False
        assert session.execute(text("SELECT format FROM numbering_config WHERE entity_kind = 'order'")).scalar() == "{BASE}{SEQ}{MM}{YY}"

    def test_safe_custom_literals_are_configuration_data(self, client, lab):
        assert put_config(client, lab.headers, format="LAB.{BASE}_{SEQ}-{MM}{YYYY}").status_code == 200
        assert make_order_code(client, lab) == "LAB.CU_01-102026"
        assert put_config(client, lab.headers, format="{YYYY}{MM}-{BASE}{SEQ}").status_code == 200
        assert make_order_code(client, lab) == "202610-CU02"  # same counter, new shape


def make_order_code(client, lab):
    r = client.post(UNIFIED, json=lab.unified(), headers=lab.headers)
    assert r.status_code == 200, r.text
    return r.json()["order"]["order_code"]


class TestConfiguration:
    def put(self, client, lab, kind="order", headers=None, **body):
        return put_config(client, headers or lab.headers, kind, **body)

    def test_a_custom_format_applies_to_new_codes_only_and_continues_the_sequence(self, client, session, lab):
        before = make_order(client, lab)
        assert self.put(client, lab, format="{BASE}-{SEQ}-{MM}{YYYY}", sequence_min_width=3).status_code == 200
        after = make_order(client, lab)
        assert before["order"]["order_code"] == "CU011026"
        assert after["order"]["order_code"] == "CU-002-102026"
        assert order_codes(session, lab)[0] == "CU011026"  # issued code untouched
        cfg = client.get(f"{NUMBERING}/config", headers=lab.headers).json()["order"]
        assert cfg["is_default"] is False and cfg["format"] == "{BASE}-{SEQ}-{MM}{YYYY}" and cfg["active"] is True

    def test_the_two_kinds_are_configured_independently(self, client, lab):
        self.put(client, lab, "sample", format="{BASE}.{SEQ}{MM}{YY}")
        body = make_order(client, lab)
        assert body["order"]["order_code"] == "CU011026"
        assert body["samples"][0]["sample_code"] == "BX.011026"

    @pytest.mark.parametrize("body", [
        {"format": "{BASE}{SEQ}"}, {"format": "{BASE}{SEQ}{MM}{YY}{YYYY}"}, {"format": "{BASE}/{SEQ}{MM}{YY}"},
        {"sequence_min_width": 0}, {"sequence_min_width": 9}, {"manual_policy": "bogus"}, {"scope": "galaxy"},
    ])
    def test_invalid_configuration_is_rejected_and_not_stored(self, client, session, lab, body):
        before = session.execute(text("SELECT count(*) FROM numbering_config")).scalar()
        r = self.put(client, lab, **body)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "numbering_invalid_config"
        assert session.execute(text("SELECT count(*) FROM numbering_config")).scalar() == before

    def test_unknown_entity_kind_is_422(self, client, lab):
        assert self.put(client, lab, "patient").status_code == 422

    def test_changes_need_manage_catalog_but_reading_needs_only_lab_read(self, client, lab):
        for role, can_write in (("lab_tech", False), ("viewer", False), ("billing", False), ("assistant", False), ("admin", True)):
            headers = lab.headers_for(role)
            assert client.get(f"{NUMBERING}/config", headers=headers).status_code == 200
            assert self.put(client, lab, headers=headers).status_code == (200 if can_write else 403), role

    def test_unauthenticated_requests_are_refused(self, client):
        assert client.get(f"{NUMBERING}/config").status_code in (401, 403)
        assert client.put(f"{NUMBERING}/config/order", json={}).status_code in (401, 403)
        assert client.post(f"{NUMBERING}/preview", json={"entity_kind": "order"}).status_code in (401, 403)

    def test_configuration_is_tenant_scoped(self, client, session, lab, other):
        self.put(client, lab, format="{BASE}-{SEQ}{MM}{YY}")
        configure(session, other)
        assert client.get(f"{NUMBERING}/config", headers=other.headers).json()["order"]["format"] == "{BASE}{SEQ}{MM}{YY}"
        assert client.post(UNIFIED, json=other.unified(), headers=other.headers).json()["order"]["order_code"] == "CU011026"

    def test_changes_are_audited_with_old_and_new_values(self, client, session, lab):
        self.put(client, lab, "sample", format="{BASE}-{SEQ}{MM}{YY}", manual_policy="forbidden", start_value=7, scope="tenant")
        rows = session.exec(select(AuditLog).where(AuditLog.action == "NUMBERING.CONFIG_UPDATED")).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.tenant_id == lab.tenant.id and row.actor_user_id == lab.user.id
        assert row.old_values["format"] == "{BASE}{SEQ}{MM}{YY}" and row.old_values["start_value"] == 1
        assert row.new_values["manual_policy"] == "forbidden" and row.new_values["start_value"] == 7
        assert row.event_metadata["entity_kind"] == "sample"

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
        configs = session.execute(text("SELECT count(*) FROM numbering_config")).scalar()
        for _ in range(5):
            self.preview(client, lab, entity_kind="order", scope_id=str(lab.study_type.id))
            self.preview(client, lab, entity_kind="sample", scope_id=str(lab.sample_types["BX"].id), format="{BASE}-{SEQ}{MM}{YY}")
        assert counter_rows(session) == 0 and ledger_rows(session) == 0
        assert session.execute(text("SELECT count(*) FROM numbering_config")).scalar() == configs
        assert make_order(client, lab)["order"]["order_code"] == "CU011026"

    def test_a_candidate_format_is_validated_without_saving_it(self, client, session, lab):
        ok = self.preview(client, lab, entity_kind="order", format="{BASE}-{SEQ}{MM}{YYYY}", sequence_min_width=3, scope_id=str(lab.study_type.id)).json()
        assert ok["valid"] and ok["examples"][0]["code"] == "CU-001102026" and ok["estimate"]["code"] == "CU-001102026"
        bad = self.preview(client, lab, entity_kind="order", format="{BASE}{SEQ}").json()
        assert bad["valid"] is False and bad["errors"] and bad["examples"] == []
        assert client.get(f"{NUMBERING}/config", headers=lab.headers).json()["order"]["format"] == "{BASE}{SEQ}{MM}{YY}"

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
        # registration never types a code: no manual-code path survives in the routes
        for gone in ("claim_manual_codes", "audit_manual_code", "check_mode", "normalize_supplied", "supplied_"):
            assert gone not in src, gone

    def test_every_creation_route_requires_a_key_and_claims_before_doing_any_work(self):
        src = self.source()
        assert src.count("idempotency.validate_key(") == 3 and src.count("idempotency.begin(") == 3
        assert src.count("idempotency.store(") == 3 and src.count("idempotency.verify_resources(") == 3
        for route in ("def create_order(", "def create_sample(", "def create_order_with_samples("):
            body = src[src.index(route):]
            assert body.index("idempotency.begin(") < body.index("numbering.allocate_batch(")  # claim precedes allocation

    def test_the_receipt_is_stored_before_the_single_commit_of_each_route(self):
        src = self.source()
        for route in ("def create_order(", "def create_sample(", "def create_order_with_samples("):
            body = src[src.index(route):]
            body = body[:body.index("@router.")] if "@router." in body else body
            assert body.index("idempotency.store(") < body.index("session.commit()")

    def test_the_ledger_has_no_foreign_key_to_orders_samples_or_catalogs(self, session):
        fks = session.execute(text(
            "SELECT ccu.table_name FROM information_schema.table_constraints tc "
            "JOIN information_schema.constraint_column_usage ccu ON tc.constraint_name = ccu.constraint_name "
            "WHERE tc.table_name IN ('numbering_allocation', 'numbering_counter') AND tc.constraint_type = 'FOREIGN KEY'"
        )).scalars().all()
        assert set(fks) == {"tenant"}
