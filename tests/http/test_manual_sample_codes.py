"""Hotfix 1.4.3: hand-typed sample codes at registration, and read-only code suggestions for the form.

Policy (owner, 2026-11-11): a sample row of the registration form may carry its own code. A typed code needs
`lab:enter_manual_code`, follows the manual-reuse policy of the post-registration correction (it may repeat or
reuse an existing, issued or retired code) and consumes NO automatic number. Rows without one are still
generated. The form asks for a suggestion first; it reserves nothing.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlmodel import select

from app.models.audit import AuditLog
from app.schemas.laboratory import OrderUnifiedCreate
from app.services import idempotency

from .numbering_support import (  # noqa: F401
    fresh_client,
    NUMBERING, SAMPLES, UNIFIED, Lab, configure, counter_rows, freeze, ledger_rows, sample_codes, set_counter,
)

SUGGEST = f"{NUMBERING}/sample-code-suggestions"


@pytest.fixture
def lab(session, monkeypatch):
    freeze(monkeypatch)  # 2026-10-15 18:00 UTC
    lab = Lab(session, "Alfa", sample_types=("BX", "TJ"))
    configure(session, lab)
    return lab


def register(client, lab, rows, headers=None, expect=200, **extra):
    """`rows`: list of (type code, typed code | None[, receipt date])."""
    payload = lab.unified(sample_codes=[r[0] for r in rows], **extra)
    for sample, row in zip(payload["samples"], rows):
        if row[1] is not None:
            sample["sample_code"] = row[1]
        if len(row) > 2:
            sample["received_at"] = row[2]
    res = client.post(UNIFIED, json=payload, headers=headers or lab.headers)
    assert res.status_code == expect, res.text
    return res.json()


def suggest(client, lab, items, headers=None, **extra):
    body = {"branch_id": str(lab.branch.id), "items": items, **extra}
    res = client.post(SUGGEST, json=body, headers=headers or lab.headers)
    assert res.status_code == 200, res.text
    return res.json()


def item(lab, key, type_code="BX", received="2026-10-15"):
    return {"key": key, "sample_type_id": str(lab.sample_types[type_code].id), "received_at": received}


def counter_value(session, lab, type_code="BX"):
    return session.execute(text(
        "SELECT coalesce(max(last_value), 0) FROM numbering_counter WHERE tenant_id = :t AND entity_kind = 'sample' "
        "AND scope_id = :s"), {"t": str(lab.tenant.id), "s": str(lab.sample_types[type_code].id)}).scalar()


class TestTypedCodeAtRegistration:
    def test_a_typed_row_is_stored_as_typed_and_takes_no_automatic_number(self, client, session, lab):
        body = register(client, lab, [("BX", None), ("BX", "MAN-7"), ("BX", None)])
        assert [s["sample_code"] for s in body["samples"]] == ["BX011026", "MAN-7", "BX021026"]
        assert counter_value(session, lab) == 2  # only the two generated rows moved the counter
        # ledger: the order + the two generated samples; the typed code has no ledger row
        assert ledger_rows(session) == 3
        assert session.execute(text("SELECT count(*) FROM numbering_allocation WHERE code = 'MAN-7'")).scalar() == 0

    def test_the_next_registration_continues_from_the_generated_rows_only(self, client, session, lab):
        register(client, lab, [("BX", "MAN-1"), ("BX", "MAN-2")])
        assert counter_value(session, lab) == 0 and sample_codes(session, lab) == ["MAN-1", "MAN-2"]
        body = register(client, lab, [("BX", None)])
        assert body["samples"][0]["sample_code"] == "BX011026"

    def test_a_blank_typed_code_means_automatic(self, client, lab):
        body = register(client, lab, [("BX", "   "), ("BX", "")])
        assert [s["sample_code"] for s in body["samples"]] == ["BX011026", "BX021026"]

    def test_surrounding_spaces_are_trimmed(self, client, lab):
        assert register(client, lab, [("BX", "  AB-1  ")])["samples"][0]["sample_code"] == "AB-1"

    @pytest.mark.parametrize("bad", ["has space", "ñandú", "a/b", "x" * 101, "A;B", 12345])
    def test_an_invalid_typed_code_is_refused_and_nothing_is_created(self, client, session, lab, bad):
        before = (counter_rows(session), ledger_rows(session), sample_codes(session, lab))
        res = client.post(UNIFIED, json=_with_code(lab, bad), headers=lab.headers)
        assert res.status_code == 422, res.text
        assert res.json()["detail"][0]["type"] == "sample_code_invalid"
        assert (counter_rows(session), ledger_rows(session), sample_codes(session, lab)) == before

    def test_a_generated_code_never_equals_a_code_typed_in_the_same_lot(self, client, session, lab):
        body = register(client, lab, [("BX", None), ("BX", "BX021026"), ("BX", None)])
        # BX021026 was typed, so the second generated row skips it (the number is still consumed by the skip)
        assert [s["sample_code"] for s in body["samples"]] == ["BX011026", "BX021026", "BX031026"]
        assert counter_value(session, lab) == 3

    def test_a_typed_code_equal_to_the_next_generated_one_is_kept_and_later_generation_skips_it(self, client, session, lab):
        register(client, lab, [("BX", "BX011026")])
        assert register(client, lab, [("BX", None)])["samples"][0]["sample_code"] == "BX021026"


class TestRepeatedAndReusedCodes:
    """The reuse policy of the hotfix: a typed code may repeat or reuse an issued code."""

    def test_the_same_code_may_be_typed_on_two_rows_of_one_lot(self, client, session, lab):
        body = register(client, lab, [("BX", "DUP-1"), ("TJ", "DUP-1")])
        assert [s["sample_code"] for s in body["samples"]] == ["DUP-1", "DUP-1"]
        assert sample_codes(session, lab) == ["DUP-1", "DUP-1"]

    def test_a_code_already_issued_by_numbering_may_be_reused_by_hand(self, client, session, lab):
        first = register(client, lab, [("BX", None)])
        issued = first["samples"][0]["sample_code"]
        again = register(client, lab, [("BX", issued)])
        assert again["samples"][0]["sample_code"] == issued
        assert sample_codes(session, lab).count(issued) == 2
        # the reuse neither consumed nor rewound anything: generation still moves forward
        assert register(client, lab, [("BX", None)])["samples"][0]["sample_code"] == "BX021026"

    def test_a_code_retired_by_a_correction_may_be_typed_again(self, client, session, lab):
        body = register(client, lab, [("BX", None)])
        old = body["samples"][0]["sample_code"]
        res = client.post(
            f"{SAMPLES}{body['samples'][0]['id']}/code-correction",
            json={"sample_code": "FIXED-1", "reason": "Corrección de prueba"}, headers=lab.headers)
        assert res.status_code == 200, res.text
        assert register(client, lab, [("BX", old)])["samples"][0]["sample_code"] == old

    def test_the_standalone_route_accepts_a_repeated_code_too(self, client, session, lab):
        order_id = register(client, lab, [("BX", "REP-1")])["order"]["id"]
        res = client.post(SAMPLES, json=lab.sample_payload(order_id, sample_code="REP-1"), headers=lab.headers)
        assert res.status_code == 200 and res.json()["sample_code"] == "REP-1"
        assert sample_codes(session, lab) == ["REP-1", "REP-1"]


class TestPermissions:
    def test_a_user_without_the_capability_cannot_type_a_code_and_nothing_is_created(self, client, session, lab):
        assistant = lab.headers_for("assistant")  # may create orders/samples, does NOT hold lab:enter_manual_code
        before = (counter_rows(session), ledger_rows(session))
        res = client.post(UNIFIED, json=_with_code(lab, "MAN-1"), headers=assistant)
        assert res.status_code == 403 and "lab:enter_manual_code" in res.text
        assert (counter_rows(session), ledger_rows(session)) == before
        assert session.execute(text('SELECT count(*) FROM "order"')).scalar() == 0

    def test_the_same_user_still_registers_with_generated_codes(self, client, lab):
        assistant = lab.headers_for("assistant")
        body = register(client, lab, [("BX", None), ("BX", None)], headers=assistant)
        assert [s["sample_code"] for s in body["samples"]] == ["BX011026", "BX021026"]

    def test_a_blank_code_from_that_user_is_automatic_not_a_privileged_capture(self, client, lab):
        assistant = lab.headers_for("assistant")
        assert register(client, lab, [("BX", "  ")], headers=assistant)["samples"][0]["sample_code"] == "BX011026"

    def test_the_standalone_route_applies_the_same_gate(self, client, session, lab):
        order_id = register(client, lab, [("BX", None)])["order"]["id"]
        assistant = lab.headers_for("assistant")
        res = client.post(SAMPLES, json=lab.sample_payload(order_id, sample_code="MAN-1"), headers=assistant)
        assert res.status_code == 403
        assert sample_codes(session, lab) == ["BX011026"]
        ok = client.post(SAMPLES, json=lab.sample_payload(order_id), headers=assistant)
        assert ok.status_code == 200 and ok.json()["sample_code"] == "BX021026"

    def test_a_capability_alone_does_not_create_samples(self, client, session, lab):
        viewer = lab.headers_for("viewer")
        res = client.post(UNIFIED, json=_with_code(lab, "MAN-1"), headers=viewer)
        assert res.status_code == 403 and "lab:create" in res.text


class TestStandaloneSample:
    def test_a_typed_code_takes_no_number_and_audits(self, client, session, lab):
        order_id = register(client, lab, [("BX", None)])["order"]["id"]
        res = client.post(SAMPLES, json=lab.sample_payload(order_id, sample_code="HAND-9"), headers=lab.headers)
        assert res.status_code == 200 and res.json()["sample_code"] == "HAND-9"
        assert counter_value(session, lab) == 1
        assert client.post(SAMPLES, json=lab.sample_payload(order_id), headers=lab.headers).json()["sample_code"] == "BX021026"

    def test_a_typed_code_on_a_wrongly_formed_row_is_refused(self, client, lab):
        order_id = register(client, lab, [("BX", None)])["order"]["id"]
        res = client.post(SAMPLES, json=lab.sample_payload(order_id, sample_code="no good"), headers=lab.headers)
        assert res.status_code == 422


class TestAuditAndProvenance:
    def test_a_typed_row_is_audited_and_marked_in_the_registration_event(self, client, session, lab):
        body = register(client, lab, [("BX", "MAN-1"), ("BX", None)])
        typed, generated = body["samples"]
        rows = session.exec(select(AuditLog).where(AuditLog.action == "SAMPLE.MANUAL_CODE_CAPTURED")).all()
        assert [str(r.entity_id) for r in rows] == [typed["id"]]
        events = dict(session.execute(text(
            "SELECT sample_id::text, event_metadata->>'code_source' FROM order_event WHERE event_type = 'SAMPLE_CREATED'"
        )).all())
        assert events[typed["id"]] == "manual" and events[generated["id"]] is None


class TestCompatibilityScheme:
    def test_a_typed_code_with_numbering_inactive(self, client, session):
        legacy = Lab(session, "Legado", sample_types=("BX",))  # no configuration: compatibility scheme
        body = register(client, legacy, [("BX", None), ("BX", "OLD-1"), ("BX", None)])
        assert [s["sample_code"] for s in body["samples"]] == ["BX-1", "OLD-1", "BX-2"]


class TestIdempotency:
    def test_a_replay_of_the_same_typed_registration_creates_once(self, client, session, lab):
        payload = lab.unified(("BX", "BX"))
        payload["samples"][0]["sample_code"] = "MAN-1"
        key = {"Idempotency-Key": "k-manual-0001"}
        first = client.post(UNIFIED, json=payload, headers={**lab.headers, **key})
        again = client.post(UNIFIED, json=payload, headers={**lab.headers, **key})
        assert first.status_code == again.status_code == 200
        assert [s["sample_code"] for s in again.json()["samples"]] == ["MAN-1", "BX011026"]
        assert sample_codes(session, lab) == ["BX011026", "MAN-1"]

    def test_the_same_key_with_a_different_typed_code_is_a_conflict(self, client, lab):
        key = {"Idempotency-Key": "k-manual-0002"}
        a = lab.unified(("BX",))
        b = lab.unified(("BX",))
        b["samples"][0]["sample_code"] = "MAN-1"
        assert client.post(UNIFIED, json=a, headers={**lab.headers, **key}).status_code == 200
        assert client.post(UNIFIED, json=b, headers={**lab.headers, **key}).status_code == 409

    def test_a_request_from_an_older_client_keeps_its_fingerprint(self, lab):
        """Rows that never send `sample_code` hash as they did before the field existed."""
        payload = OrderUnifiedCreate(**lab.unified(("BX", "TJ")))
        with_none = idempotency.fingerprint(payload)
        dumped = payload.model_dump(mode="json")
        assert dumped.pop("report_template_id") is None  # omitted while null since 1.4.1
        for row in dumped["samples"]:
            assert row.pop("sample_code") is None
        import hashlib
        import json
        canonical = json.dumps(dumped, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        assert with_none == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TestSuggestions:
    def test_one_lot_follows_the_sequence_without_reserving_anything(self, client, session, lab):
        counters, ledger = counter_rows(session), ledger_rows(session)
        out = suggest(client, lab, [item(lab, "a"), item(lab, "b"), item(lab, "c", "TJ"), item(lab, "d")])
        assert [s["code"] for s in out["suggestions"]] == ["BX011026", "BX021026", "TJ011026", "BX031026"]
        assert all(s["reserved"] is False for s in out["suggestions"])
        assert (counter_rows(session), ledger_rows(session)) == (counters, ledger)  # nothing written

    def test_what_is_suggested_for_a_lot_is_what_registration_issues(self, client, lab):
        rows = [("BX", None, "2026-10-15"), ("TJ", None, "2026-10-15"), ("BX", None, "2026-11-02"), ("BX", None, "2026-10-20")]
        items = [item(lab, str(i), r[0], r[2]) for i, r in enumerate(rows)]
        suggested = [s["code"] for s in suggest(client, lab, items)["suggestions"]]
        issued = [s["sample_code"] for s in register(client, lab, rows)["samples"]]
        assert suggested == issued == ["BX011026", "TJ011026", "BX011126", "BX021026"]

    def test_it_continues_from_the_current_counter(self, client, session, lab):
        set_counter(session, lab, "sample", lab.sample_types["BX"], 41)
        assert suggest(client, lab, [item(lab, "a")])["suggestions"][0]["code"] == "BX421026"

    def test_occupied_codes_are_skipped_like_the_real_allocation_does(self, client, lab):
        register(client, lab, [("BX", "BX011026")])  # typed code that collides with the next generated one
        assert suggest(client, lab, [item(lab, "a")])["suggestions"][0]["code"] == "BX021026"

    def test_codes_typed_in_the_same_lot_are_never_suggested(self, client, lab):
        out = suggest(client, lab, [item(lab, "a"), item(lab, "b")], reserved_codes=["BX011026"])
        assert [s["code"] for s in out["suggestions"]] == ["BX021026", "BX031026"]
        issued = register(client, lab, [("BX", None), ("BX", None), ("BX", "BX011026")])
        assert [s["sample_code"] for s in issued["samples"]][:2] == ["BX021026", "BX031026"]

    def test_the_receipt_date_is_needed_by_the_monthly_scheme(self, client, lab):
        out = suggest(client, lab, [item(lab, "a", received=None), item(lab, "b", received=" "), item(lab, "c")])
        s = out["suggestions"]
        assert [x["reason"] for x in s] == ["receipt_date_required", "receipt_date_required", None]
        assert s[0]["code"] is None and s[2]["code"] == "BX011026"  # a row without a date takes no number

    def test_the_month_of_each_code_comes_from_its_own_receipt_date(self, client, lab):
        out = suggest(client, lab, [item(lab, "a", received="2026-09-30"), item(lab, "b", received="2026-10-01")])
        assert [(x["code"], x["period_month"]) for x in out["suggestions"]] == [("BX010926", 9), ("BX011026", 10)]

    @pytest.mark.parametrize("bad", ["not-a-date", "20261015", "1697000000", "2026-13-40"])
    def test_a_malformed_date_is_reported_not_guessed(self, client, lab, bad):
        out = suggest(client, lab, [item(lab, "a", received=bad)])
        assert out["suggestions"][0]["code"] is None and out["suggestions"][0]["reason"] == "receipt_date_invalid"

    def test_the_compatibility_scheme_needs_no_date(self, client, session):
        legacy = Lab(session, "Legado", sample_types=("BX",))
        out = suggest(client, legacy, [item(legacy, "a", received=None), item(legacy, "b", received=None)])
        assert out["scheme"] == "legacy" and [s["code"] for s in out["suggestions"]] == ["BX-1", "BX-2"]

    def test_a_type_of_another_laboratory_or_an_inactive_one_has_no_suggestion(self, client, session, lab):
        other = Lab(session, "Beta", sample_types=("ZZ",))
        foreign = {"key": "x", "sample_type_id": str(other.sample_types["ZZ"].id), "received_at": "2026-10-15"}
        lab.sample_types["TJ"].is_active = False
        session.add(lab.sample_types["TJ"])
        session.commit()
        out = suggest(client, lab, [foreign, item(lab, "y", "TJ"), item(lab, "z")])
        assert [(s["key"], s["reason"]) for s in out["suggestions"]] == [
            ("x", "sample_type_not_found"), ("y", "sample_type_inactive"), ("z", None)]

    def test_a_branch_of_another_laboratory_is_not_found(self, client, session, lab):
        other = Lab(session, "Beta", sample_types=("ZZ",))
        res = client.post(SUGGEST, json={"branch_id": str(other.branch.id), "items": []}, headers=lab.headers)
        assert res.status_code == 404

    def test_duplicate_row_keys_and_oversized_lots_are_refused(self, client, lab):
        dup = client.post(SUGGEST, json={"items": [item(lab, "a"), item(lab, "a")]}, headers=lab.headers)
        assert dup.status_code == 422
        big = client.post(SUGGEST, json={"items": [item(lab, str(i)) for i in range(101)]}, headers=lab.headers)
        assert big.status_code == 422

    def test_reading_needs_only_lab_read_and_an_anonymous_caller_is_refused(self, client, lab):
        assert suggest(client, lab, [item(lab, "a")], headers=lab.headers_for("viewer"))["suggestions"][0]["code"] == "BX011026"
        assert client.post(SUGGEST, json={"items": []}).status_code in (401, 403)

    def test_a_branch_scoped_numbering_needs_the_branch(self, client, session, lab):
        configure(session, lab, "sample", scope="branch", format="{BASE}{BRANCH}{SEQ}{MM}{YY}")
        res = client.post(SUGGEST, json={"items": [item(lab, "a")]}, headers=lab.headers)
        assert res.json()["suggestions"][0]["reason"] == "branch_required"
        ok = suggest(client, lab, [item(lab, "a")])["suggestions"][0]
        assert ok["code"] and ok["code"].startswith("BX" + lab.branch.code)


def _with_code(lab, code):
    payload = lab.unified(("BX",))
    payload["samples"][0]["sample_code"] = code
    return payload


class TestBranchListCarriesItsTimezone:
    """Hotfix 1.4.3: the registration forms default the receipt date to "today" in the numbering zone, which
    for a branch-scoped numbering is the branch's own zone."""

    def test_the_branches_of_a_tenant_list_their_timezone(self, client, lab):
        res = client.get(f"/api/v1/tenants/{lab.tenant.id}/branches", headers=lab.headers)
        assert res.status_code == 200, res.text
        row = next(b for b in res.json() if b["id"] == str(lab.branch.id))
        assert row["timezone"] == lab.branch.timezone and row["code"] == lab.branch.code
