"""Sample monthly numbering by RECEIPT date over HTTP (issue #33, follow-up of #8).

Contract: celuma-engineering/specs/laboratory/monthly-numbering.md ("Period: date source and timezone").
A sample's rendered month/year AND its counter/ledger period come from `received_at` interpreted in
the numbering timezone; orders keep the allocation-instant policy. Real PostgreSQL; synthetic data.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.models.audit import AuditLog

from .numbering_support import (  # noqa: F401
    NUMBERING, ORDERS, RECEIVED, SAMPLES, UNIFIED, Lab, configure, counter_rows, freeze, fresh_client, ledger_rows,
    run_parallel, sample_codes, set_counter,
)

KEY = "Idempotency-Key"


@pytest.fixture
def lab(session, monkeypatch):
    # Registration time: 2026-10-01 12:00 UTC = Oct 1, 06:00 in Mexico City (so "registered in October").
    freeze(monkeypatch, 2026, 10, 1, 12)
    lab = Lab(session, "Alfa", sample_types=("BX", "TJ"))
    configure(session, lab)
    return lab


def counts(session):
    return {
        "orders": session.execute(text('SELECT count(*) FROM "order"')).scalar(),
        "samples": session.execute(text("SELECT count(*) FROM sample")).scalar(),
        "invoices": session.execute(text("SELECT count(*) FROM invoice")).scalar(),
        "events": session.execute(text("SELECT count(*) FROM order_event")).scalar(),
        "ledger": ledger_rows(session),
        "counter_rows": counter_rows(session),
        "counter_sum": session.execute(text("SELECT coalesce(sum(last_value), 0) FROM numbering_counter")).scalar(),
        "audit": session.execute(text("SELECT count(*) FROM audit_log")).scalar(),
        "receipts": session.execute(text("SELECT count(*) FROM idempotency_receipt")).scalar(),
    }


def make_order(client, lab):
    r = client.post(ORDERS, json=lab.order_payload(), headers=lab.headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def sample(client, lab, order, received, type_code="BX", **extra):
    payload = lab.sample_payload(order, type_code, received_at=received, **extra)
    r = client.post(SAMPLES, json=payload, headers=lab.headers)
    assert r.status_code == 200, r.text
    return r.json()


def unified(client, lab, received, types=None, **extra):
    types = types or ("BX",) * len(received)
    r = client.post(UNIFIED, json=lab.unified(sample_codes=types, received=list(received), **extra), headers=lab.headers)
    assert r.status_code == 200, r.text
    return r.json()


def ledger_periods(session, kind="sample"):
    return session.execute(text(
        "SELECT code, period_year, period_month FROM numbering_allocation WHERE entity_kind = :k ORDER BY code"
    ), {"k": kind}).all()


def stored_received(session, code):
    return session.execute(text("SELECT received_at FROM sample WHERE sample_code = :c"), {"c": code}).scalar()


class TestReceiptVersusRegistrationMonth:
    def test_the_issue_example_standalone(self, client, session, lab):
        order = make_order(client, lab)  # registered 2026-10-01
        body = sample(client, lab, order, "2026-09-30")
        assert body["sample_code"] == "BX010926"  # September receipt, NOT BX011026
        assert ledger_periods(session) == [("BX010926", 2026, 9)]
        counters = session.execute(text(
            "SELECT period_year, period_month, last_value FROM numbering_counter WHERE entity_kind = 'sample'")).all()
        assert counters == [(2026, 9, 1)]  # the counter period is the receipt period; October's is untouched

    def test_the_issue_example_unified_and_the_order_keeps_its_own_policy(self, client, session, lab):
        body = unified(client, lab, ["2026-09-30"])
        assert body["samples"][0]["sample_code"] == "BX010926"
        assert body["order"]["order_code"] == "CU011026"  # ORDER numbering is unchanged: the allocation instant
        assert ledger_periods(session, "order") == [("CU011026", 2026, 10)]

    def test_a_current_month_receipt_matches_the_registration_month(self, client, lab):
        assert unified(client, lab, ["2026-10-01"])["samples"][0]["sample_code"] == "BX011026"

    def test_month_and_year_boundaries(self, client, session, lab):
        for received, code in [
            ("2026-12-31", "BX011226"), ("2027-01-01", "BX010127"), ("2026-02-28", "BX010226"),
            ("2026-03-01", "BX010326"), ("2027-12-31", "BX011227"), ("2028-01-01", "BX010128"),
        ]:
            assert unified(client, lab, [received])["samples"][0]["sample_code"] == code
        periods = {c: (y, m) for c, y, m in ledger_periods(session)}
        assert periods == {
            "BX011226": (2026, 12), "BX010127": (2027, 1), "BX010226": (2026, 2),
            "BX010326": (2026, 3), "BX011227": (2027, 12), "BX010128": (2028, 1),
        }

    def test_no_year_range_is_imposed_and_a_two_digit_year_collision_is_skipped_not_reused(self, client, session, lab):
        # {YY} repeats every century: 1999-12 / 2099-12 and 2000-01 / 2100-01 render the same text. The
        # permanent ledger and live-row checks (existing collision protection) skip the occupied string.
        codes = [unified(client, lab, [d])["samples"][0]["sample_code"]
                 for d in ("2000-01-15", "2100-01-15", "1999-12-31", "2099-12-31", "2100-01-31")]
        assert codes == ["BX010100", "BX020100", "BX011299", "BX021299", "BX030100"]
        assert len(set(sample_codes(session, lab))) == 5
        assert {c: (y, m) for c, y, m in ledger_periods(session)}["BX020100"] == (2100, 1)  # its OWN period, skipped value

    def test_future_dates_are_accepted(self, client, lab):
        assert unified(client, lab, ["2031-05-01"])["samples"][0]["sample_code"] == "BX010531"

    def test_the_sample_time_does_not_matter_only_the_calendar_date_in_the_zone(self, client, lab):
        assert unified(client, lab, ["2026-09-30T23:59:59"])["samples"][0]["sample_code"] == "BX010926"
        assert unified(client, lab, ["2026-10-01T00:00:00"])["samples"][0]["sample_code"] == "BX011026"

    def test_audit_and_created_timestamps_stay_the_processing_time(self, client, session, lab):
        order = make_order(client, lab)
        sample(client, lab, order, "2026-09-30")
        created = session.execute(text("SELECT created_at FROM order_event WHERE sample_id IS NOT NULL")).scalar()
        assert created is not None and created.year >= 2026  # actual processing time, never the receipt date
        row = session.exec(select(AuditLog).order_by(AuditLog.created_at.desc())).first()
        assert row is None or row.created_at.date() != datetime(2026, 9, 30).date()


class TestBackdatedAllocationResumesTheCounter:
    def test_an_earlier_period_resumes_its_own_counter_without_reset_or_rewind(self, client, session, lab):
        order = make_order(client, lab)
        oct1 = sample(client, lab, order, "2026-10-05")["sample_code"]
        oct2 = sample(client, lab, order, "2026-10-20")["sample_code"]
        sep1 = sample(client, lab, order, "2026-09-10")["sample_code"]  # backdated: opens September
        sep2 = sample(client, lab, order, "2026-09-02")["sample_code"]  # resumes September, no reset
        oct3 = sample(client, lab, order, "2026-10-31")["sample_code"]  # October was never rewound
        assert [oct1, oct2, sep1, sep2, oct3] == ["BX011026", "BX021026", "BX010926", "BX020926", "BX031026"]
        assert len(set(sample_codes(session, lab))) == 5

    def test_a_pre_existing_counter_is_continued_not_restarted(self, client, session, lab):
        set_counter(session, lab, "sample", lab.sample_types["BX"], 7, year=2026, month=9)
        order = make_order(client, lab)
        assert sample(client, lab, order, "2026-09-30")["sample_code"] == "BX080926"

    def test_types_keep_independent_counters_per_period(self, client, lab):
        order = make_order(client, lab)
        assert sample(client, lab, order, "2026-09-30", "BX")["sample_code"] == "BX010926"
        assert sample(client, lab, order, "2026-09-30", "TJ")["sample_code"] == "TJ010926"
        assert sample(client, lab, order, "2026-09-29", "BX")["sample_code"] == "BX020926"

    def test_a_rendered_collision_is_skipped_never_reused(self, client, session, lab):
        order = make_order(client, lab)
        first = sample(client, lab, order, "2026-09-30")["sample_code"]
        session.execute(text("UPDATE numbering_counter SET last_value = 0 WHERE entity_kind = 'sample'"))
        session.commit()  # simulate a counter that lost its place: the ledger and rows still protect the code
        again = sample(client, lab, order, "2026-09-30")["sample_code"]
        assert first == "BX010926" and again == "BX020926"

    def test_start_value_and_width_apply_to_the_receipt_period(self, client, session, lab):
        configure(session, lab, "sample", start_value=5, sequence_min_width=3, format="{BASE}-{SEQ}{MM}{YY}")
        order = make_order(client, lab)
        assert sample(client, lab, order, "2026-09-30")["sample_code"] == "BX-0050926"


class TestMixedPeriodsInOneUnifiedRequest:
    def test_each_sample_numbers_in_its_own_period_in_one_atomic_request(self, client, session, lab):
        body = unified(client, lab, ["2026-09-30", "2026-10-01", "2026-09-15", "2026-09-30"], types=("BX", "BX", "TJ", "BX"))
        assert [s["sample_code"] for s in body["samples"]] == ["BX010926", "BX011026", "TJ010926", "BX020926"]
        assert body["order"]["order_code"] == "CU011026"
        assert {(c, y, m) for c, y, m in ledger_periods(session)} == {
            ("BX010926", 2026, 9), ("BX011026", 2026, 10), ("TJ010926", 2026, 9), ("BX020926", 2026, 9)}

    def test_a_year_boundary_inside_one_request(self, client, lab):
        body = unified(client, lab, ["2026-12-31", "2027-01-01"])
        assert [s["sample_code"] for s in body["samples"]] == ["BX011226", "BX010127"]

    def test_one_invalid_date_among_valid_ones_creates_nothing(self, fresh_client, session, lab):
        before = counts(session)
        payload = lab.unified(sample_codes=("BX", "BX"), received=["2026-09-30", "1790000000"])
        r = fresh_client.post(UNIFIED, json=payload, headers=lab.headers)
        assert r.status_code == 422
        assert counts(session) == before


class TestDateInterpretationAndTimezones:
    """Date-only and timezone-less values ARE the local calendar date; an explicit offset is an instant."""

    def test_a_date_only_value_is_the_calendar_date_and_is_stored_as_that_day(self, client, session, lab):
        body = unified(client, lab, ["2026-10-01"])
        code = body["samples"][0]["sample_code"]
        assert code == "BX011026"
        assert stored_received(session, code) == datetime(2026, 10, 1, 0, 0)

    def test_the_old_ui_value_midnight_utc_is_an_instant_and_lands_in_the_previous_local_month(self, client, session, lab):
        # `2026-10-01T00:00:00Z` is Sep 30, 18:00 in Mexico City: the exact shift the web app used to cause.
        code = unified(client, lab, ["2026-10-01T00:00:00Z"])["samples"][0]["sample_code"]
        assert code == "BX010926"
        assert stored_received(session, code) == datetime(2026, 9, 30, 18, 0)  # stored as the local wall clock

    @pytest.mark.parametrize("value,code", [
        ("2026-10-01T05:59:59Z", "BX010926"), ("2026-10-01T06:00:00Z", "BX011026"),
        ("2026-09-30T20:00:00-06:00", "BX010926"), ("2026-10-01T00:00:00-06:00", "BX011026"),
        ("2026-10-01T08:00:00+14:00", "BX010926"),  # Oct 1 08:00 at +14 = Sep 30 18:00 in Mexico City
    ])
    def test_offset_instants_are_converted_to_the_laboratory_zone(self, client, lab, value, code):
        assert unified(client, lab, [value])["samples"][0]["sample_code"] == code

    def test_the_laboratory_zone_is_configuration_not_the_browser_server_or_utc(self, client, session, lab):
        configure(session, lab, "sample", timezone="Pacific/Auckland")  # UTC+13 in October
        # Sep 30 20:00 UTC = Oct 1 09:00 in Auckland
        assert unified(client, lab, ["2026-09-30T20:00:00Z"])["samples"][0]["sample_code"] == "BX011026"
        # a date-only value is not shifted by any zone
        assert unified(client, lab, ["2026-09-30"])["samples"][0]["sample_code"] == "BX010926"

    def test_two_branches_share_the_laboratory_period_for_tenant_wide_counters(self, client, session, lab):
        from app.models.tenant import Branch
        norte = Branch(tenant_id=lab.tenant.id, code="NORTE", name="Norte", timezone="Pacific/Auckland")
        session.add(norte)
        session.commit()
        session.refresh(norte)
        instant = "2026-10-31T12:00:00Z"  # Oct 31 in Mexico City, Nov 1 in Auckland
        a = unified(client, lab, [instant])["samples"][0]["sample_code"]
        payload = lab.unified(received=[instant])
        payload["branch_id"] = str(norte.id)
        b = client.post(UNIFIED, json=payload, headers=lab.headers).json()["samples"][0]["sample_code"]
        assert (a, b) == ("BX011026", "BX021026")  # one laboratory zone, one counter

    def test_a_branch_scoped_counter_uses_the_branchs_own_zone(self, client, session, lab):
        from app.models.tenant import Branch
        norte = Branch(tenant_id=lab.tenant.id, code="NORTE", name="Norte", timezone="Pacific/Auckland")
        session.add(norte)
        session.commit()
        session.refresh(norte)
        configure(session, lab, "sample", scope="branch", format="{BASE}{BRANCH}{SEQ}{MM}{YY}")
        instant = "2026-10-31T12:00:00Z"
        mx = unified(client, lab, [instant])["samples"][0]["sample_code"]
        payload = lab.unified(received=[instant])
        payload["branch_id"] = str(norte.id)
        nz = client.post(UNIFIED, json=payload, headers=lab.headers).json()["samples"][0]["sample_code"]
        assert (mx, nz) == ("BXMAIN011026", "BXNORTE011126")

    def test_order_and_sample_zones_stay_independent(self, client, session, lab, monkeypatch):
        configure(session, lab, "order", timezone="UTC")
        configure(session, lab, "sample", timezone="America/Mexico_City")
        freeze(monkeypatch, 2026, 11, 1, 3, 30)  # Nov 1 in UTC, Oct 31 in Mexico City
        body = unified(client, lab, ["2026-10-31"])
        assert body["order"]["order_code"] == "CU011126"       # allocation instant, order zone (UTC)
        assert body["samples"][0]["sample_code"] == "BX011026"  # receipt date, sample zone


class TestReceiptDateIsRequiredForNewSamples:
    BAD = [
        pytest.param({"_omit": True}, id="missing"),
        pytest.param(None, id="null"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param("not-a-date", id="garbage"),
        pytest.param("2026-13-01", id="month-13"),
        pytest.param("2026-02-30", id="feb-30"),
        pytest.param(1790000000, id="epoch-number"),
        pytest.param("1790000000", id="epoch-string"),
        pytest.param("1790000000.5", id="epoch-fraction-string"),
        pytest.param("20260930", id="basic-format-digits"),
        pytest.param("9/30/2026", id="us-format"),
        pytest.param("0000-01-01", id="year-zero"),
        pytest.param(True, id="boolean"),
        pytest.param({"a": 1}, id="object"),
        pytest.param("0001-01-01T00:00:00+14:00", id="offset-conversion-leaves-year-1"),
        pytest.param("9999-12-31T23:59:59-14:00", id="offset-conversion-leaves-year-9999"),
    ]

    @staticmethod
    def with_bad(payload, bad, holder=None):
        target = payload if holder is None else payload["samples"][holder]
        if isinstance(bad, dict) and bad.get("_omit"):
            target.pop("received_at", None)
        else:
            target["received_at"] = bad
        return payload

    @pytest.mark.parametrize("bad", BAD)
    def test_standalone_rejects_without_any_side_effect(self, fresh_client, session, lab, bad):
        order = make_order(fresh_client, lab)
        before = counts(session)
        payload = self.with_bad(lab.sample_payload(order), bad)
        r = fresh_client.post(SAMPLES, json=payload, headers=lab.headers)
        assert r.status_code == 422, r.text
        assert counts(session) == before  # no sample, counter, ledger, invoice, event, audit or receipt
        assert "BX" not in " ".join(sample_codes(session, lab))

    @pytest.mark.parametrize("bad", BAD)
    def test_unified_rejects_without_any_side_effect(self, fresh_client, session, lab, bad):
        before = counts(session)
        payload = self.with_bad(lab.unified(sample_codes=("BX", "TJ")), bad, holder=1)
        r = fresh_client.post(UNIFIED, json=payload, headers=lab.headers)
        assert r.status_code == 422, r.text
        assert counts(session) == before  # not even the order or the valid first sample

    def test_a_rejected_request_does_not_burn_the_key(self, fresh_client, session, lab):
        key = f"key-{uuid.uuid4().hex}"
        bad = lab.unified()
        bad["samples"][0].pop("received_at")
        assert fresh_client.post(UNIFIED, json=bad, headers={**lab.headers, KEY: key}).status_code == 422
        good = fresh_client.post(UNIFIED, json=lab.unified(), headers={**lab.headers, KEY: key})
        assert good.status_code == 200 and good.json()["samples"][0]["sample_code"] == "BX011026"

    def test_the_registration_time_and_today_are_never_a_fallback(self, fresh_client, session, lab):
        payload = lab.unified()
        del payload["samples"][0]["received_at"]
        assert fresh_client.post(UNIFIED, json=payload, headers=lab.headers).status_code == 422
        assert sample_codes(session, lab) == []

    def test_collection_date_is_independent_and_optional(self, client, session, lab):
        order = make_order(client, lab)
        body = sample(client, lab, order, "2026-09-30")  # no collected_at at all
        assert body["sample_code"] == "BX010926" and body["collected_at"] is None
        later = sample(client, lab, order, "2026-09-30", collected_at="2026-08-31T10:00:00")
        assert later["sample_code"] == "BX020926"  # collected in August, numbered by September receipt
        assert later["collected_at"].startswith("2026-08-31")

    def test_inactive_numbering_still_requires_the_date_but_keeps_the_compatibility_code(self, client, session, monkeypatch):
        freeze(monkeypatch, 2026, 10, 1, 12)
        legacy = Lab(session, "Gama", sample_types=("BX",))  # no configuration row: not activated
        order = client.post(ORDERS, json=legacy.order_payload(), headers=legacy.headers).json()["id"]
        missing = legacy.sample_payload(order)
        missing.pop("received_at")
        assert client.post(SAMPLES, json=missing, headers=legacy.headers).status_code == 422
        ok = client.post(SAMPLES, json=legacy.sample_payload(order, received_at="2026-09-30"), headers=legacy.headers)
        assert ok.status_code == 200 and ok.json()["sample_code"] == "BX-1"
        assert ok.json()["received_at"].startswith("2026-09-30")

    def test_legacy_rows_without_a_receipt_date_stay_readable_and_unchanged(self, client, session, lab):
        order = make_order(client, lab)
        body = sample(client, lab, order, "2026-09-30")
        session.execute(text("UPDATE sample SET received_at = NULL WHERE id = :i"), {"i": body["id"]})
        session.commit()
        detail = client.get(f"/api/v1/laboratory/samples/{body['id']}", headers=lab.headers)
        assert detail.status_code == 200 and detail.json()["received_at"] is None
        assert detail.json()["sample_code"] == "BX010926"


class TestIdempotency:
    def test_a_replay_in_another_month_returns_the_original_sample_and_allocates_nothing(self, client, session, lab, monkeypatch):
        key = f"key-{uuid.uuid4().hex}"
        payload = lab.unified(received=["2026-09-30"])
        first = client.post(UNIFIED, json=payload, headers={**lab.headers, KEY: key})
        assert first.status_code == 200
        after = counts(session)
        freeze(monkeypatch, 2027, 2, 3, 12)  # the month (and year) changed
        again = client.post(UNIFIED, json=payload, headers={**lab.headers, KEY: key})
        assert again.status_code == 200 and again.headers["Idempotent-Replayed"] == "true"
        assert again.json() == first.json() and counts(session) == after

    def test_standalone_replay_in_another_month(self, client, session, lab, monkeypatch):
        order = make_order(client, lab)
        key = f"key-{uuid.uuid4().hex}"
        payload = lab.sample_payload(order, received_at="2026-09-30")
        first = client.post(SAMPLES, json=payload, headers={**lab.headers, KEY: key}).json()
        after = counts(session)
        freeze(monkeypatch, 2026, 12, 20)
        again = client.post(SAMPLES, json=payload, headers={**lab.headers, KEY: key})
        assert again.json() == first and again.headers["Idempotent-Replayed"] == "true" and counts(session) == after

    def test_the_same_key_with_a_changed_receipt_date_is_the_payload_conflict(self, client, session, lab):
        order = make_order(client, lab)
        key = f"key-{uuid.uuid4().hex}"
        ok = client.post(SAMPLES, json=lab.sample_payload(order, received_at="2026-09-30"), headers={**lab.headers, KEY: key})
        assert ok.status_code == 200
        after = counts(session)
        changed = client.post(SAMPLES, json=lab.sample_payload(order, received_at="2026-10-01"), headers={**lab.headers, KEY: key})
        assert changed.status_code == 409 and changed.json()["detail"]["code"] == "idempotency_key_reused"
        assert counts(session) == after

    def test_unified_changed_receipt_date_conflicts(self, client, session, lab):
        key = f"key-{uuid.uuid4().hex}"
        assert client.post(UNIFIED, json=lab.unified(received=["2026-09-30"]), headers={**lab.headers, KEY: key}).status_code == 200
        r = client.post(UNIFIED, json=lab.unified(received=["2026-09-29"]), headers={**lab.headers, KEY: key})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "idempotency_key_reused"

    def test_authorization_is_checked_before_a_replay(self, client, session, lab):
        key = f"key-{uuid.uuid4().hex}"
        payload = lab.unified(received=["2026-09-30"])
        assert client.post(UNIFIED, json=payload, headers={**lab.headers, KEY: key}).status_code == 200
        viewer = lab.headers_for("viewer")
        assert client.post(UNIFIED, json=payload, headers={**viewer, KEY: key}).status_code == 403

    def test_an_equivalent_spelling_of_the_same_date_is_the_same_request(self, client, session, lab):
        key = f"key-{uuid.uuid4().hex}"
        first = client.post(UNIFIED, json=lab.unified(received=["2026-09-30"]), headers={**lab.headers, KEY: key})
        again = client.post(UNIFIED, json=lab.unified(received=["2026-09-30T00:00:00"]), headers={**lab.headers, KEY: key})
        assert again.status_code == 200 and again.headers["Idempotent-Replayed"] == "true" and again.json() == first.json()


class TestAllocationEvidence:
    def test_the_ledger_records_the_receipt_period_the_entity_and_the_scheme(self, client, session, lab):
        body = unified(client, lab, ["2026-09-30"])
        row = session.execute(text(
            "SELECT period_year, period_month, sequence_value, code, entity_id, scheme FROM numbering_allocation "
            "WHERE entity_kind = 'sample'")).one()
        assert (row.period_year, row.period_month, row.sequence_value, row.code) == (2026, 9, 1, "BX010926")
        assert str(row.entity_id) == body["samples"][0]["id"] and row.scheme == "monthly"

    def test_a_later_edit_of_the_receipt_date_never_changes_an_issued_code(self, client, session, lab):
        order = make_order(client, lab)
        body = sample(client, lab, order, "2026-09-30")
        session.execute(text("UPDATE sample SET received_at = '2026-11-15' WHERE id = :i"), {"i": body["id"]})
        session.commit()
        assert sample_codes(session, lab) == ["BX010926"]
        assert [c for c, _, _ in ledger_periods(session)] == ["BX010926"]


class TestPreview:
    def preview(self, client, lab, **body):
        r = client.post(f"{NUMBERING}/preview", json={"entity_kind": "sample", "scope_id": str(lab.sample_types["BX"].id), **body}, headers=lab.headers)
        assert r.status_code == 200, r.text
        return r.json()

    def test_a_sample_preview_uses_the_receipt_date_and_names_the_basis(self, client, session, lab):
        before = counts(session)
        p = self.preview(client, lab, reference_date="2026-09-30")
        assert (p["period_year"], p["period_month"], p["date_basis"], p["estimate"]["code"]) == (2026, 9, "receipt_date", "BX010926")
        assert p["estimate"]["reserved"] is False
        assert counts(session) == before  # a preview consumes nothing

    def test_the_preview_agrees_with_the_real_allocation(self, client, lab):
        order = make_order(client, lab)
        sample(client, lab, order, "2026-09-10")
        predicted = self.preview(client, lab, reference_date="2026-09-30")["estimate"]["code"]
        assert sample(client, lab, order, "2026-09-30")["sample_code"] == predicted == "BX020926"

    def test_the_preview_honours_the_zone_for_offset_values(self, client, lab):
        p = self.preview(client, lab, reference_date="2026-10-01T00:00:00Z")
        assert (p["period_month"], p["timezone"]) == (9, "America/Mexico_City")

    def test_an_order_preview_ignores_the_reference_date(self, client, lab):
        r = client.post(f"{NUMBERING}/preview", json={"entity_kind": "order", "reference_date": "2020-01-01", "scope_id": str(lab.study_type.id)}, headers=lab.headers).json()
        assert (r["period_year"], r["period_month"], r["date_basis"]) == (2026, 10, "allocation_instant")

    def test_without_a_date_a_sample_preview_says_it_is_the_current_month(self, client, lab):
        p = self.preview(client, lab)
        assert (p["period_month"], p["date_basis"]) == (10, "allocation_instant")

    @pytest.mark.parametrize("bad", ["1790000000", 1790000000, True, "20260930", "x", "", "2026-13-01",
                                     "0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"])
    def test_a_provided_invalid_reference_date_is_an_explicit_422_never_a_500(self, client, lab, bad):
        r = client.post(f"{NUMBERING}/preview", json={"entity_kind": "sample", "reference_date": bad}, headers=lab.headers)
        assert r.status_code == 422, r.text

    @pytest.mark.parametrize("value,code", [("1999-12-31", "BX011299"), ("2100-01-01", "BX010100")])
    def test_years_outside_the_two_digit_window_are_previewed_not_refused(self, client, lab, value, code):
        assert self.preview(client, lab, reference_date=value)["estimate"]["code"] == code


class TestConcurrency:
    def test_parallel_samples_across_periods_are_unique_and_contiguous_per_period(self, fresh_client, session, lab):
        order = fresh_client.post(ORDERS, json=lab.order_payload(), headers=lab.headers).json()["id"]
        dates = ["2026-09-30", "2026-10-02", "2026-08-15"] * 4
        jobs = [lambda c, d=d: c.post(SAMPLES, json=lab.sample_payload(order, received_at=d), headers=lab.headers) for d in dates]
        results = run_parallel(jobs)
        bad = [r for r in results if isinstance(r, Exception) or r.status_code != 200]
        assert not bad, [getattr(r, "text", r) for r in bad]
        codes = sorted(r.json()["sample_code"] for r in results)
        expected = sorted(f"BX{n:02d}{mm}26" for mm in ("08", "09", "10") for n in range(1, 5))
        assert codes == expected and len(set(codes)) == 12

    def test_unified_requests_taking_periods_in_opposite_order_do_not_deadlock(self, fresh_client, session, lab):
        forward = lab.unified(sample_codes=("BX", "TJ", "BX", "TJ"), received=["2026-09-30", "2026-10-02", "2026-10-03", "2026-09-29"])
        backward = lab.unified(sample_codes=("TJ", "BX", "TJ", "BX"), received=["2026-09-29", "2026-10-03", "2026-10-02", "2026-09-30"])
        jobs = [(lambda c, p=(forward if i % 2 == 0 else backward): c.post(UNIFIED, json=p, headers=lab.headers)) for i in range(12)]
        results = run_parallel(jobs)
        bad = [r for r in results if isinstance(r, Exception) or r.status_code != 200]
        assert not bad, [getattr(r, "text", r) for r in bad]
        codes = sample_codes(session, lab)
        assert len(codes) == 48 and len(set(codes)) == 48
        for type_code in ("BX", "TJ"):
            for mm in ("09", "10"):
                assert sorted(c for c in codes if c.startswith(type_code) and c.endswith(f"{mm}26")) == sorted(
                    f"{type_code}{n:02d}{mm}26" for n in range(1, 13)), (type_code, mm)

    def test_a_backdated_and_a_current_request_in_parallel_never_share_a_code(self, fresh_client, session, lab):
        order = fresh_client.post(ORDERS, json=lab.order_payload(), headers=lab.headers).json()["id"]
        dates = ["2026-09-30", "2026-10-01"] * 6
        jobs = [lambda c, d=d: c.post(SAMPLES, json=lab.sample_payload(order, received_at=d), headers=lab.headers) for d in dates]
        results = run_parallel(jobs)
        assert all(not isinstance(r, Exception) and r.status_code == 200 for r in results)
        codes = [r.json()["sample_code"] for r in results]
        assert len(set(codes)) == 12
        assert sorted(c for c in codes if c.endswith("0926")) == [f"BX{n:02d}0926" for n in range(1, 7)]
        assert sorted(c for c in codes if c.endswith("1026")) == [f"BX{n:02d}1026" for n in range(1, 7)]

    def test_parallel_retries_of_one_logical_request_create_one_sample(self, fresh_client, session, lab):
        order = fresh_client.post(ORDERS, json=lab.order_payload(), headers=lab.headers).json()["id"]
        key = f"key-{uuid.uuid4().hex}"
        payload = lab.sample_payload(order, received_at="2026-09-30")
        results = run_parallel([lambda c: c.post(SAMPLES, json=payload, headers={**lab.headers, KEY: key}) for _ in range(6)])
        assert all(not isinstance(r, Exception) and r.status_code == 200 for r in results)
        assert {r.json()["sample_code"] for r in results} == {"BX010926"}
        assert sample_codes(session, lab) == ["BX010926"]
