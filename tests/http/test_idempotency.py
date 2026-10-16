"""Idempotent order/sample creation (issue #8; ADR 0014).

Standalone order, standalone sample and unified multi-sample creation require an
`Idempotency-Key`. Real PostgreSQL throughout; concurrency with threads.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.models.audit import AuditLog

from .factories import auth_headers, create_user
from .numbering_support import (  # noqa: F401
    NUMBERING, ORDERS, SAMPLES, UNIFIED, Lab, configure, counter_rows, delete_order_and_samples, freeze, fresh_client,
    ledger_rows, order_codes, run_parallel, sample_codes,
)

KEY = "Idempotency-Key"


class ResponseLost(Exception):
    """The client never saw the response (connection dropped after the server committed)."""


def send_and_lose(client, url, **kw):
    """Run the request to completion on the server, then discard what it answered."""
    client.post(url, **kw)
    raise ResponseLost()


@pytest.fixture
def lab(session, monkeypatch):
    freeze(monkeypatch)
    lab = Lab(session, "Alfa", sample_types=("BX", "TJ"))
    configure(session, lab)
    return lab


@pytest.fixture
def other(session):
    lab = Lab(session, "Beta", sample_types=("BX",))
    configure(session, lab)
    return lab


def h(lab_or_headers, key=None):
    base = lab_or_headers.headers if hasattr(lab_or_headers, "headers") else lab_or_headers
    return {**base, KEY: key or f"key-{uuid.uuid4().hex}"}


def counts(session):
    return {
        "orders": session.execute(text('SELECT count(*) FROM "order"')).scalar(),
        "samples": session.execute(text("SELECT count(*) FROM sample")).scalar(),
        "invoices": session.execute(text("SELECT count(*) FROM invoice")).scalar(),
        "events": session.execute(text("SELECT count(*) FROM order_event")).scalar(),
        "ledger": ledger_rows(session),
        "counter": session.execute(text("SELECT coalesce(sum(last_value), 0) FROM numbering_counter")).scalar(),
        "audit": session.execute(text("SELECT count(*) FROM audit_log")).scalar(),
        "receipts": session.execute(text("SELECT count(*) FROM idempotency_receipt")).scalar(),
    }


class TestKeyIsRequired:
    @pytest.mark.parametrize("url,payload", [
        (UNIFIED, lambda lab: lab.unified()),
        (ORDERS, lambda lab: lab.order_payload()),
    ])
    def test_a_creation_without_a_key_is_refused_and_creates_nothing(self, client, session, lab, url, payload):
        client.auto_key = False
        before = counts(session)
        r = client.post(url, json=payload(lab), headers=lab.headers)
        assert r.status_code == 428 and r.json()["detail"]["code"] == "idempotency_key_required"
        assert counts(session) == before

    def test_the_sample_route_requires_it_too(self, client, session, lab):
        order = client.post(ORDERS, json=lab.order_payload(), headers=lab.headers).json()["id"]
        client.auto_key = False
        r = client.post(SAMPLES, json=lab.sample_payload(order), headers=lab.headers)
        assert r.status_code == 428

    @pytest.mark.parametrize("bad", ["short", "a" * 129, "has space in it", "semi;colon;key", "new\nline-key"])
    def test_a_malformed_key_is_refused(self, client, session, lab, bad):
        before = counts(session)
        client.auto_key = False
        r = client.post(UNIFIED, json=lab.unified(), headers={**lab.headers, KEY: bad})
        assert r.status_code in (422, 428) and counts(session) == before

    def test_a_blank_key_counts_as_missing(self, client, lab):
        client.auto_key = False
        assert client.post(UNIFIED, json=lab.unified(), headers={**lab.headers, KEY: "   "}).status_code == 428

    def test_authentication_and_permission_come_before_the_key_check_side_effects(self, client, session, lab):
        client.auto_key = False
        r = client.post(UNIFIED, json=lab.unified(), headers={KEY: "k" * 12})
        assert r.status_code in (401, 403)
        r = client.post(UNIFIED, json=lab.unified(), headers=h(lab.headers_for("viewer")))
        assert r.status_code == 403


class TestReplay:
    def test_unified_replay_returns_the_original_resources_with_no_new_side_effects(self, client, session, lab):
        headers = h(lab)
        payload = lab.unified(sample_codes=("BX", "TJ", "BX"))
        first = client.post(UNIFIED, json=payload, headers=headers)
        assert first.status_code == 200 and not first.headers.get("Idempotent-Replayed")
        after_first = counts(session)
        again = client.post(UNIFIED, json=payload, headers=headers)
        assert again.status_code == 200 and again.headers["Idempotent-Replayed"] == "true"
        assert again.json() == first.json()  # same ids, same codes, same order of samples
        assert counts(session) == after_first  # no second order, invoice, event, ledger row, counter step, audit
        third = client.post(UNIFIED, json=payload, headers=headers)
        assert third.json() == first.json() and counts(session) == after_first

    def test_standalone_order_replay(self, client, session, lab):
        headers = h(lab)
        first = client.post(ORDERS, json=lab.order_payload(), headers=headers).json()
        snapshot = counts(session)
        again = client.post(ORDERS, json=lab.order_payload(), headers=headers)
        assert again.json() == first and again.headers["Idempotent-Replayed"] == "true" and counts(session) == snapshot
        assert first["order_code"] == "CU011026"

    def test_standalone_sample_replay(self, client, session, lab):
        order = client.post(ORDERS, json=lab.order_payload(), headers=lab.headers).json()["id"]
        headers = h(lab)
        payload = lab.sample_payload(order)
        first = client.post(SAMPLES, json=payload, headers=headers).json()
        snapshot = counts(session)
        again = client.post(SAMPLES, json=payload, headers=headers)
        assert again.json() == first and again.headers["Idempotent-Replayed"] == "true" and counts(session) == snapshot
        assert first["sample_code"] == "BX011026"

    def test_a_new_key_is_a_new_logical_submission_and_gets_the_next_number(self, client, lab):
        a = client.post(UNIFIED, json=lab.unified(), headers=h(lab)).json()["order"]["order_code"]
        b = client.post(UNIFIED, json=lab.unified(), headers=h(lab)).json()["order"]["order_code"]
        assert (a, b) == ("CU011026", "CU021026")

    def test_a_manual_code_replays_without_a_second_audit_row(self, client, session, lab):
        headers = h(lab)
        payload = lab.unified(order_code="MAN-1", sample_codes=(("BX", "S-1"),))
        client.post(UNIFIED, json=payload, headers=headers)
        audits = session.execute(text("SELECT count(*) FROM audit_log WHERE action = 'NUMBERING.MANUAL_CODE_USED'")).scalar()
        assert audits == 2
        client.post(UNIFIED, json=payload, headers=headers)
        assert session.execute(text("SELECT count(*) FROM audit_log WHERE action = 'NUMBERING.MANUAL_CODE_USED'")).scalar() == 2

    def test_a_manual_code_retry_is_replayed_not_rejected_as_a_duplicate(self, client, lab):
        headers = h(lab)
        payload = lab.unified(order_code="MAN-DUP")
        assert client.post(UNIFIED, json=payload, headers=headers).status_code == 200
        again = client.post(UNIFIED, json=payload, headers=headers)
        assert again.status_code == 200 and again.headers["Idempotent-Replayed"] == "true"
        # whereas a NEW submission with the same manual code is a genuine duplicate
        assert client.post(UNIFIED, json=payload, headers=h(lab)).status_code == 400


class TestPayloadMismatch:
    def test_the_same_key_with_a_different_payload_fails_explicitly_and_creates_nothing(self, client, session, lab):
        headers = h(lab)
        client.post(UNIFIED, json=lab.unified(), headers=headers)
        before = counts(session)
        r = client.post(UNIFIED, json=lab.unified(sample_codes=("BX", "BX")), headers=headers)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "idempotency_key_reused"
        assert counts(session) == before

    @pytest.mark.parametrize("change", [
        lambda p: p.update(notes="otra"), lambda p: p["samples"][0].update(notes="x"),
        lambda p: p.update(order_code="M-1"), lambda p: p["samples"].append(dict(p["samples"][0])),
    ])
    def test_any_field_changes_the_fingerprint(self, client, session, lab, change):
        headers = h(lab)
        payload = lab.unified()
        client.post(UNIFIED, json=payload, headers=headers)
        changed = lab.unified()
        change(changed)
        assert client.post(UNIFIED, json=changed, headers=headers).status_code == 409

    def test_json_key_order_does_not_change_the_fingerprint(self, client, lab):
        headers = h(lab)
        payload = lab.unified()
        first = client.post(UNIFIED, json=payload, headers=headers).json()
        reordered = dict(reversed(list(payload.items())))
        again = client.post(UNIFIED, json=reordered, headers=headers)
        assert again.status_code == 200 and again.json() == first

    def test_only_a_hash_of_the_request_is_stored(self, client, session, lab):
        payload = lab.unified(notes="texto-sensible-del-caso", sample_codes=(("BX", "M-SECRET"),))
        client.post(UNIFIED, json=payload, headers=h(lab))
        row = session.execute(text("SELECT request_fingerprint, result::text, resources::text FROM idempotency_receipt")).one()
        assert len(row[0]) == 64 and int(row[0], 16) >= 0
        stored = " ".join(row)
        assert "texto-sensible-del-caso" not in stored and "Dr. Sintético" not in stored
        assert "M-SECRET" in row[1]  # the code a key produced is the one thing kept, with ids


class TestIsolation:
    def test_the_same_key_by_another_user_is_an_independent_submission(self, client, session, lab):
        key = f"shared-{uuid.uuid4().hex}"
        a = client.post(UNIFIED, json=lab.unified(), headers=h(lab, key)).json()["order"]["order_code"]
        other_user = auth_headers(create_user(session, lab.tenant, email="otro@alfa.invalid", roles=("superuser",)))
        b = client.post(UNIFIED, json=lab.unified(), headers=h(other_user, key))
        assert b.status_code == 200 and not b.headers.get("Idempotent-Replayed")
        assert b.json()["order"]["order_code"] != a

    def test_the_same_key_in_another_tenant_is_independent(self, client, lab, other):
        key = f"shared-{uuid.uuid4().hex}"
        a = client.post(UNIFIED, json=lab.unified(), headers=h(lab, key))
        b = client.post(UNIFIED, json=other.unified(), headers=h(other, key))
        assert a.status_code == b.status_code == 200 and not b.headers.get("Idempotent-Replayed")
        assert a.json()["order"]["id"] != b.json()["order"]["id"]

    def test_the_same_key_for_another_operation_is_independent(self, client, session, lab):
        key = f"shared-{uuid.uuid4().hex}"
        order = client.post(ORDERS, json=lab.order_payload(), headers=h(lab, key))
        unified = client.post(UNIFIED, json=lab.unified(), headers=h(lab, key))
        assert order.status_code == unified.status_code == 200 and not unified.headers.get("Idempotent-Replayed")
        assert session.execute(text("SELECT count(*) FROM idempotency_receipt")).scalar() == 2

    def test_another_tenant_can_never_replay_my_receipt(self, client, session, lab, other):
        key = f"shared-{uuid.uuid4().hex}"
        mine = client.post(UNIFIED, json=lab.unified(), headers=h(lab, key)).json()
        theirs = client.post(UNIFIED, json=other.unified(), headers=h(other, key)).json()
        assert theirs["order"]["tenant_id"] == str(other.tenant.id) != mine["order"]["tenant_id"]

    def test_the_database_enforces_one_receipt_per_tenant_actor_operation_key(self, client, session, lab):
        client.post(UNIFIED, json=lab.unified(), headers=h(lab, "dup-key-123456"))
        with pytest.raises(IntegrityError):
            session.execute(text(
                "INSERT INTO idempotency_receipt (id, tenant_id, actor_user_id, operation, idempotency_key, request_fingerprint, "
                "response_status, result, resources, created_at) SELECT gen_random_uuid(), tenant_id, actor_user_id, operation, "
                "idempotency_key, request_fingerprint, 200, result, resources, now() FROM idempotency_receipt"))
        session.rollback()


class TestAuthorizationOnReplay:
    def test_a_user_who_lost_the_permission_cannot_replay(self, client, session, lab):
        assistant = lab.headers_for("assistant")
        headers = h(assistant)
        payload = lab.unified()
        assert client.post(UNIFIED, json=payload, headers=headers).status_code == 200
        session.execute(text(
            "DELETE FROM user_role WHERE role_id IN (SELECT id FROM role WHERE code = 'assistant')"))
        session.commit()
        r = client.post(UNIFIED, json=payload, headers=headers)
        assert r.status_code == 403 and not r.headers.get("Idempotent-Replayed")

    def test_a_replay_with_another_tenants_ids_is_refused(self, client, lab, other):
        headers = h(lab)
        client.post(UNIFIED, json=lab.unified(), headers=headers)
        forged = lab.unified()
        forged["tenant_id"] = str(other.tenant.id)
        assert client.post(UNIFIED, json=forged, headers=headers).status_code == 403
        forged = lab.unified()
        forged["branch_id"] = str(other.branch.id)
        assert client.post(UNIFIED, json=forged, headers=headers).status_code == 403

    def test_a_replay_after_the_branch_was_removed_from_the_tenant_view_is_refused(self, client, session, lab):
        headers = h(lab)
        payload = lab.unified()
        client.post(UNIFIED, json=payload, headers=headers)
        session.execute(text("UPDATE branch SET tenant_id = :t WHERE id = :b"),
                        {"t": str(Lab(session, "Gamma").tenant.id), "b": str(lab.branch.id)})
        session.commit()
        assert client.post(UNIFIED, json=payload, headers=headers).status_code == 403


class TestRollbackAndRetry:
    def test_a_failed_request_leaves_no_receipt_and_the_same_key_can_retry(self, fresh_client, session, lab, monkeypatch):
        from fastapi import HTTPException

        import app.api.v1.laboratory as laboratory

        real = laboratory._materialize_default_reviewer
        monkeypatch.setattr(laboratory, "_materialize_default_reviewer", lambda *a, **k: (_ for _ in ()).throw(HTTPException(409, "boom")))
        headers = h(lab)
        payload = lab.unified(sample_codes=("BX", "TJ"))
        r = fresh_client.post(UNIFIED, json=payload, headers=headers)
        assert r.status_code == 409
        assert counts(session) == {k: 0 for k in counts(session)} | {"audit": counts(session)["audit"]}
        monkeypatch.setattr(laboratory, "_materialize_default_reviewer", real)
        ok = fresh_client.post(UNIFIED, json=payload, headers=headers)  # the SAME key: nothing was committed
        assert ok.status_code == 200 and not ok.headers.get("Idempotent-Replayed")
        assert ok.json()["order"]["order_code"] == "CU011026"
        assert counts(session)["receipts"] == 1 and counts(session)["orders"] == 1

    def test_a_validation_rejection_leaves_nothing_and_a_corrected_payload_may_reuse_the_key(self, fresh_client, session, lab):
        headers = h(lab)
        bad = lab.unified()
        bad["samples"][0]["sample_type_id"] = str(uuid.uuid4())
        assert fresh_client.post(UNIFIED, json=bad, headers=headers).status_code == 404
        assert counts(session)["receipts"] == 0 and counts(session)["counter"] == 0
        assert fresh_client.post(UNIFIED, json=lab.unified(), headers=headers).status_code == 200

    def test_a_manual_code_rejection_is_not_remembered_either(self, fresh_client, session, lab):
        fresh_client.post(UNIFIED, json=lab.unified(order_code="TAKEN"), headers=h(lab))
        headers = h(lab)
        assert fresh_client.post(UNIFIED, json=lab.unified(order_code="TAKEN"), headers=headers).status_code == 400
        assert counts(session)["receipts"] == 1  # only the first, successful one
        # the rejected attempt left nothing behind, so that key can carry a corrected submission
        assert fresh_client.post(UNIFIED, json=lab.unified(order_code="FREE"), headers=headers).status_code == 200


class TestResponseLostAfterCommit:
    def test_unified(self, fresh_client, session, lab):
        headers = h(lab)
        payload = lab.unified(sample_codes=("BX", "TJ"))
        with pytest.raises(ResponseLost):
            send_and_lose(fresh_client, UNIFIED, json=payload, headers=headers)
        assert counts(session)["orders"] == 1  # it DID commit
        retry = fresh_client.post(UNIFIED, json=payload, headers=headers)
        assert retry.status_code == 200 and retry.headers["Idempotent-Replayed"] == "true"
        assert retry.json()["order"]["order_code"] == "CU011026"
        assert [s["sample_code"] for s in retry.json()["samples"]] == ["BX011026", "TJ011026"]
        c = counts(session)
        assert c["orders"] == 1 and c["samples"] == 2 and c["invoices"] == 1 and c["ledger"] == 3 and c["receipts"] == 1

    def test_standalone_order_and_sample(self, fresh_client, session, lab):
        headers = h(lab)
        with pytest.raises(ResponseLost):
            send_and_lose(fresh_client, ORDERS, json=lab.order_payload(), headers=headers)
        order = fresh_client.post(ORDERS, json=lab.order_payload(), headers=headers).json()
        assert order["order_code"] == "CU011026" and counts(session)["orders"] == 1
        sheaders = h(lab)
        with pytest.raises(ResponseLost):
            send_and_lose(fresh_client, SAMPLES, json=lab.sample_payload(order["id"]), headers=sheaders)
        sample = fresh_client.post(SAMPLES, json=lab.sample_payload(order["id"]), headers=sheaders).json()
        assert sample["sample_code"] == "BX011026" and counts(session)["samples"] == 1

    def test_retry_after_the_month_and_the_configuration_changed_still_returns_the_original_codes(self, fresh_client, session, lab, monkeypatch):
        headers = h(lab)
        payload = lab.unified()
        with pytest.raises(ResponseLost):
            send_and_lose(fresh_client, UNIFIED, json=payload, headers=headers)
        from datetime import datetime, timezone

        from app.services import numbering

        monkeypatch.setattr(numbering, "_now", lambda: datetime(2026, 12, 2, 12, tzinfo=timezone.utc))
        configure(session, lab, format="{BASE}-{SEQ}{MM}{YY}", start_value=900, scope="tenant")
        retry = fresh_client.post(UNIFIED, json=payload, headers=headers)
        assert retry.headers["Idempotent-Replayed"] == "true"
        assert retry.json()["order"]["order_code"] == "CU011026"  # not CU-900-1226
        assert counts(session)["orders"] == 1

    def test_retry_after_the_numbering_zone_changed_keeps_the_original_period_and_codes(self, fresh_client, session, lab, monkeypatch):
        headers = h(lab)
        payload = lab.unified()
        with pytest.raises(ResponseLost):
            send_and_lose(fresh_client, UNIFIED, json=payload, headers=headers)   # October, Mexico City
        from datetime import datetime, timezone

        from app.services import numbering

        monkeypatch.setattr(numbering, "_now", lambda: datetime(2026, 11, 1, 3, 30, tzinfo=timezone.utc))
        configure(session, lab, timezone="UTC")  # the same instant is now November in the laboratory zone
        retry = fresh_client.post(UNIFIED, json=payload, headers=headers)
        assert retry.headers["Idempotent-Replayed"] == "true"
        assert retry.json()["order"]["order_code"] == "CU011026" and counts(session)["orders"] == 1
        # a NEW submission follows the new zone, and the retired October number is untouched
        assert fresh_client.post(UNIFIED, json=payload, headers=h(lab)).json()["order"]["order_code"] == "CU011126"

    def test_retry_after_a_catalog_type_was_deactivated_still_replays(self, fresh_client, session, lab):
        headers = h(lab)
        payload = lab.unified()
        first = fresh_client.post(UNIFIED, json=payload, headers=headers).json()
        session.execute(text("UPDATE sample_type SET is_active = false"))
        session.commit()
        again = fresh_client.post(UNIFIED, json=payload, headers=headers)
        assert again.status_code == 200 and again.headers["Idempotent-Replayed"] == "true"
        assert [(s["id"], s["sample_code"]) for s in again.json()["samples"]] == [(s["id"], s["sample_code"]) for s in first["samples"]]
        assert again.json()["order"] == first["order"]


class TestDeletedResources:
    def test_a_replay_of_a_deleted_order_is_refused_not_recreated(self, client, session, lab):
        headers = h(lab)
        payload = lab.unified()
        first = client.post(UNIFIED, json=payload, headers=headers).json()
        delete_order_and_samples(session, first["order"]["id"])
        before = counts(session)
        r = client.post(UNIFIED, json=payload, headers=headers)
        assert r.status_code == 410 and r.json()["detail"]["code"] == "idempotent_resource_deleted"
        assert counts(session) == before and order_codes(session, lab) == []
        # a genuinely new submission gets a fresh number; the retired one is never reissued
        assert client.post(UNIFIED, json=payload, headers=h(lab)).json()["order"]["order_code"] == "CU021026"

    def test_a_deleted_sample_of_a_unified_request_also_refuses_the_replay(self, client, session, lab):
        headers = h(lab)
        payload = lab.unified(sample_codes=("BX", "TJ"))
        first = client.post(UNIFIED, json=payload, headers=headers).json()
        session.execute(text("DELETE FROM order_event WHERE sample_id = :s"), {"s": first["samples"][1]["id"]})
        session.execute(text("DELETE FROM sample WHERE id = :s"), {"s": first["samples"][1]["id"]})
        session.commit()
        assert client.post(UNIFIED, json=payload, headers=headers).status_code == 410

    def test_a_deleted_sample_replay_for_the_standalone_route(self, client, session, lab):
        order = client.post(ORDERS, json=lab.order_payload(), headers=lab.headers).json()["id"]
        headers = h(lab)
        first = client.post(SAMPLES, json=lab.sample_payload(order), headers=headers).json()
        session.execute(text("DELETE FROM order_event WHERE sample_id = :s"), {"s": first["id"]})
        session.execute(text("DELETE FROM sample WHERE id = :s"), {"s": first["id"]})
        session.commit()
        assert client.post(SAMPLES, json=lab.sample_payload(order), headers=headers).status_code == 410

    def test_a_replay_shows_the_current_state_of_the_original_record(self, client, session, lab):
        order = client.post(ORDERS, json=lab.order_payload(), headers=lab.headers).json()["id"]
        headers = h(lab)
        first = client.post(SAMPLES, json=lab.sample_payload(order), headers=headers).json()
        session.execute(text("UPDATE sample SET state = 'PROCESSING' WHERE id = :s"), {"s": first["id"]})
        session.commit()
        again = client.post(SAMPLES, json=lab.sample_payload(order), headers=headers).json()
        assert again["id"] == first["id"] and again["sample_code"] == first["sample_code"] and again["state"] == "PROCESSING"


class TestConcurrentSameKey:
    def test_eight_simultaneous_submissions_with_one_key_have_exactly_one_committed_outcome(self, fresh_client, session, lab):
        key = f"race-{uuid.uuid4().hex}"
        payload = lab.unified(sample_codes=("BX", "TJ"))
        results = run_parallel([lambda c: c.post(UNIFIED, json=payload, headers=h(lab, key)) for _ in range(8)])
        assert all(not isinstance(r, Exception) and r.status_code == 200 for r in results), [getattr(r, "text", r) for r in results]
        bodies = {str(r.json()) for r in results}
        assert len(bodies) == 1  # everyone got the same order, samples and codes
        assert sum(1 for r in results if r.headers.get("Idempotent-Replayed") == "true") == 7
        c = counts(session)
        assert c["orders"] == 1 and c["samples"] == 2 and c["invoices"] == 1 and c["receipts"] == 1 and c["ledger"] == 3

    def test_simultaneous_same_key_with_different_payloads_commit_one_and_conflict_the_rest(self, fresh_client, session, lab):
        key = f"race-{uuid.uuid4().hex}"
        payloads = [lab.unified(notes=f"variante-{i}") for i in range(6)]
        results = run_parallel([(lambda c, p=p: c.post(UNIFIED, json=p, headers=h(lab, key))) for p in payloads])
        statuses = sorted(r.status_code for r in results)
        assert statuses == [200] + [409] * 5, statuses
        assert counts(session)["orders"] == 1 and counts(session)["receipts"] == 1

    def test_many_logical_submissions_each_retried_concurrently_create_one_record_each(self, fresh_client, session, lab):
        keys = [f"logical-{i}-{uuid.uuid4().hex}" for i in range(6)]
        jobs = [(lambda c, k=k: c.post(UNIFIED, json=lab.unified(notes=k), headers=h(lab, k))) for k in keys for _ in range(3)]
        results = run_parallel(jobs)
        assert all(not isinstance(r, Exception) and r.status_code == 200 for r in results), [getattr(r, "text", r) for r in results]
        codes = sorted(order_codes(session, lab))
        assert codes == sorted(f"CU{n:02d}1026" for n in range(1, 7))  # six submissions, six numbers, no gaps, no deadlock
        assert counts(session)["orders"] == 6 and counts(session)["receipts"] == 6
        assert sorted(sample_codes(session, lab)) == sorted(f"BX{n:02d}1026" for n in range(1, 7))

    def test_retries_of_opposite_sample_orders_do_not_deadlock_with_idempotency_in_play(self, fresh_client, session, lab):
        jobs = []
        for i in range(6):
            kinds = ("BX", "TJ") if i % 2 == 0 else ("TJ", "BX")
            key = f"opp-{i}-{uuid.uuid4().hex}"
            for _ in range(2):
                jobs.append(lambda c, k=key, kd=kinds: c.post(UNIFIED, json=lab.unified(sample_codes=kd), headers=h(lab, k)))
        results = run_parallel(jobs)
        assert all(not isinstance(r, Exception) and r.status_code == 200 for r in results)
        assert counts(session)["orders"] == 6 and counts(session)["samples"] == 12

    def test_manual_and_generated_submissions_with_retries_never_duplicate_a_code(self, fresh_client, session, lab):
        fresh_client.post(ORDERS, json=lab.order_payload(), headers=h(lab))  # counter at 1 -> next CU021026
        manual_key, gen_key = f"m-{uuid.uuid4().hex}", f"g-{uuid.uuid4().hex}"
        jobs = [lambda c: c.post(UNIFIED, json=lab.unified(order_code="CU021026"), headers=h(lab, manual_key)) for _ in range(2)]
        jobs += [lambda c: c.post(UNIFIED, json=lab.unified(), headers=h(lab, gen_key)) for _ in range(2)]
        results = run_parallel(jobs)
        assert not [r for r in results if isinstance(r, Exception)]
        assert all(r.status_code in (200, 400) for r in results), [r.text for r in results]
        codes = order_codes(session, lab)
        assert len(codes) == len(set(codes)) and codes.count("CU021026") <= 1


class TestAuditedAndLogs:
    def test_a_replay_writes_no_audit_and_the_key_is_not_in_audit_rows(self, client, session, lab):
        headers = h(lab, "very-private-key-123")
        client.post(UNIFIED, json=lab.unified(order_code="M-1"), headers=headers)
        n = session.execute(text("SELECT count(*) FROM audit_log")).scalar()
        client.post(UNIFIED, json=lab.unified(order_code="M-1"), headers=headers)
        assert session.execute(text("SELECT count(*) FROM audit_log")).scalar() == n
        dumped = " ".join(str(r.event_metadata) + str(r.new_values) for r in session.exec(select(AuditLog)).all())
        assert "very-private-key-123" not in dumped
