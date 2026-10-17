"""Narrow post-registration sample-code correction (#10), over real HTTP + PostgreSQL."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.models.enums import EventType, OrderStatus, ReportStatus

from .management_support import LAB, add_event, count, make_report, register, set_status
from .numbering_support import (
    SAMPLES, Lab, configure, counter_rows, freeze, fresh_client, ledger_rows, run_parallel, sample_codes,  # noqa: F401
)


@pytest.fixture
def lab(session, monkeypatch):
    freeze(monkeypatch)
    return Lab(session, "Alfa", sample_types=("BX", "TJ"))


def correct(client, lab, sample_id, code, reason="captura incorrecta", headers=None):
    return client.post(f"{LAB}/samples/{sample_id}/code-correction", json={"sample_code": code, "reason": reason},
                       headers=headers or lab.headers)


def sample_detail(client, lab, sample_id):
    return client.get(f"{LAB}/samples/{sample_id}", headers=lab.headers).json()


class TestCorrection:
    def test_changes_only_the_code_keeping_identity_and_relationships(self, client, session, lab):
        reg = register(client, lab, ("BX", "TJ"))
        sid, oid = reg["sample_ids"][0], reg["order_id"]
        before = sample_detail(client, lab, sid)
        res = correct(client, lab, sid, "LAB.7-A")
        assert res.status_code == 200, res.text
        assert res.json() == {"sample_id": sid, "old_code": reg["sample_codes"][0], "sample_code": "LAB.7-A", "unchanged": False}
        after = sample_detail(client, lab, sid)
        assert after["sample_code"] == "LAB.7-A"
        assert {k: v for k, v in after.items() if k != "sample_code"} == {k: v for k, v in before.items() if k != "sample_code"}
        assert after["order"]["id"] == oid
        assert sample_detail(client, lab, reg["sample_ids"][1])["sample_code"] == reg["sample_codes"][1]

    def test_search_and_lists_show_the_new_code_not_the_old(self, client, session, lab):
        reg = register(client, lab)
        correct(client, lab, reg["sample_ids"][0], "NUEVO9")
        found = client.get(f"{LAB}/samples/", params={"q": "NUEVO9"}, headers=lab.headers).json()["samples"]
        assert [s["id"] for s in found] == reg["sample_ids"]
        assert client.get(f"{LAB}/samples/", params={"q": reg["sample_codes"][0]}, headers=lab.headers).json()["samples"] == []

    def test_audit_trail_has_actor_reason_old_and_new(self, client, session, lab):
        reg = register(client, lab)
        correct(client, lab, reg["sample_ids"][0], "NUEVO9", reason="se rotuló mal")
        row = session.execute(text(
            "SELECT old_code, new_code, reason, corrected_by FROM sample_code_correction")).one()
        assert (row.old_code, row.new_code, row.reason, str(row.corrected_by)) == (
            reg["sample_codes"][0], "NUEVO9", "se rotuló mal", str(lab.user.id))
        audit = session.execute(text("SELECT old_values, new_values, event_metadata FROM audit_log WHERE action = 'SAMPLE.CODE_CORRECTED'")).one()
        assert audit.old_values == {"sample_code": reg["sample_codes"][0]} and audit.new_values == {"sample_code": "NUEVO9"}
        assert audit.event_metadata["reason"] == "se rotuló mal"
        event = session.execute(text("SELECT event_metadata FROM order_event WHERE event_type = 'SAMPLE_CODE_CORRECTED'")).scalar_one()
        assert event["old_code"] == reg["sample_codes"][0] and event["new_code"] == "NUEVO9"

    def test_unchanged_is_a_safe_noop(self, client, session, lab):
        reg = register(client, lab)
        res = correct(client, lab, reg["sample_ids"][0], reg["sample_codes"][0])
        assert res.status_code == 200 and res.json()["unchanged"] is True
        assert count(session, "SELECT count(*) FROM sample_code_correction") == 0

    @pytest.mark.parametrize("bad", ["", "  ", "con espacio", "a/b", "ñandú", "x" * 101, "a\nb", "{BASE}"])
    def test_code_alphabet_and_length(self, client, lab, bad):
        reg = register(client, lab)
        res = correct(client, lab, reg["sample_ids"][0], bad)
        assert res.status_code == 422, res.text
        assert res.json()["detail"]["code"] == "sample_code_invalid"

    @pytest.mark.parametrize("reason", ["", " ab ", "x" * 1001])
    def test_a_reason_is_required(self, client, lab, reason):
        reg = register(client, lab)
        assert correct(client, lab, reg["sample_ids"][0], "OK1", reason=reason).status_code == 422

    def test_permission_needs_both_update_and_the_identifier_capability(self, client, session, lab):
        reg = register(client, lab)
        sid = reg["sample_ids"][0]
        assert correct(client, lab, sid, "OK1", headers=lab.headers_for("lab_tech")).status_code == 403  # has update_sample only
        assert correct(client, lab, sid, "OK1", headers=lab.headers_for("reviewer")).status_code == 403
        assert correct(client, lab, sid, "OK1", headers=lab.headers_for("admin")).status_code == 200  # admin holds both
        state = client.get(f"{LAB}/samples/{sid}/management", headers=lab.headers_for("lab_tech")).json()
        assert state["correction"]["permitted"] is False

    def test_other_tenants_sample_is_invisible(self, client, session, lab):
        reg = register(client, lab)
        other = Lab(session, "Beta")
        assert correct(client, lab, reg["sample_ids"][0], "OK1", headers=other.headers).status_code == 404
        assert sample_detail(client, lab, reg["sample_ids"][0])["sample_code"] == reg["sample_codes"][0]


class TestUniquenessAndNonReuse:
    def test_cannot_take_a_code_in_use_anywhere_in_the_tenant(self, client, session, lab):
        a, b = register(client, lab, ("BX",)), register(client, lab, ("BX",))
        res = correct(client, lab, a["sample_ids"][0], b["sample_codes"][0])
        assert res.status_code == 409 and res.json()["detail"]["code"] == "sample_code_unavailable"

    def test_cannot_take_a_code_the_ledger_issued(self, client, session, lab):
        a = register(client, lab)
        ghost = "BX-777"  # issued by the allocator, its sample later hard-deleted by an operator
        session.execute(text(
            "INSERT INTO numbering_allocation (id, tenant_id, entity_kind, scope_id, branch_id, period_year, period_month, "
            "sequence_value, code, scheme, format, sequence_min_width, created_at) VALUES (gen_random_uuid(), :t, 'sample', "
            "gen_random_uuid(), gen_random_uuid(), 0, 0, 777, :c, 'legacy', '{BASE}-{N}', 2, now())"),
            {"t": str(lab.tenant.id), "c": ghost})
        session.commit()
        assert correct(client, lab, a["sample_ids"][0], ghost).status_code == 409

    def test_a_corrected_away_code_is_retired_for_corrections_and_for_generation(self, client, session, lab):
        a = register(client, lab)
        original = a["sample_codes"][0]
        assert original == "BX-1"
        assert correct(client, lab, a["sample_ids"][0], "LAB1").status_code == 200
        b = register(client, lab)  # generation continues; BX-1 was consumed for good
        assert b["sample_codes"] == ["BX-2"]
        res = correct(client, lab, b["sample_ids"][0], original)
        assert res.status_code == 409 and res.json()["detail"]["code"] == "sample_code_unavailable"
        # a legacy typed code that the ledger never saw is retired too
        session.execute(text("UPDATE sample SET sample_code = 'MANUAL-1' WHERE id = :i"), {"i": b["sample_ids"][0]})
        session.commit()
        assert correct(client, lab, b["sample_ids"][0], "MANUAL-2").status_code == 200
        c = register(client, lab)
        assert correct(client, lab, c["sample_ids"][0], "MANUAL-1").status_code == 409

    def test_corrections_never_touch_the_counters_or_the_ledger(self, client, session, lab):
        configure(session, lab)
        reg = register(client, lab, ("BX", "BX"))
        counters = session.execute(text("SELECT * FROM numbering_counter ORDER BY 1,2,3,4,5,6")).all()
        ledger = ledger_rows(session)
        correct(client, lab, reg["sample_ids"][0], "LAB1")
        correct(client, lab, reg["sample_ids"][1], reg["sample_codes"][0])  # taking the first sample's retired code: refused
        assert session.execute(text("SELECT * FROM numbering_counter ORDER BY 1,2,3,4,5,6")).all() == counters
        assert ledger_rows(session) == ledger
        nxt = register(client, lab)
        assert nxt["sample_codes"] == ["BX031026"]  # automatic numbering continues, no gap closed, nothing lowered

    def test_a_removed_samples_code_cannot_be_taken(self, client, session, lab):
        reg = register(client, lab, ("BX", "BX"))
        assert client.post(f"{LAB}/samples/{reg['sample_ids'][1]}/removal", json={"reason": "duplicada"},
                           headers=lab.headers).status_code == 200
        assert correct(client, lab, reg["sample_ids"][0], reg["sample_codes"][1]).status_code == 409


class TestLifecycle:
    @pytest.mark.parametrize("status", [OrderStatus.RECEIVED, OrderStatus.PROCESSING, OrderStatus.DIAGNOSIS])
    def test_allowed_from_reception_through_diagnosis(self, client, session, lab, status):
        reg = register(client, lab)
        set_status(session, reg["order_id"], status)
        assert correct(client, lab, reg["sample_ids"][0], "OK1").status_code == 200

    @pytest.mark.parametrize("status", [OrderStatus.REVIEW, OrderStatus.RELEASED, OrderStatus.CLOSED, OrderStatus.CANCELLED])
    def test_refused_from_review_onward(self, client, session, lab, status):
        reg = register(client, lab)
        set_status(session, reg["order_id"], status)
        res = correct(client, lab, reg["sample_ids"][0], "OK1")
        assert res.status_code == 409 and res.json()["detail"]["code"] == "order_not_directly_modifiable"
        assert sample_detail(client, lab, reg["sample_ids"][0])["sample_code"] == reg["sample_codes"][0]
        assert count(session, "SELECT count(*) FROM sample_code_correction") == 0

    def test_a_ready_sample_does_not_by_itself_authorize_a_late_correction(self, client, session, lab):
        reg = register(client, lab)
        set_status(session, reg["order_id"], OrderStatus.REVIEW)
        session.execute(text("UPDATE sample SET state = 'READY' WHERE id = :i"), {"i": reg["sample_ids"][0]})
        session.commit()
        assert correct(client, lab, reg["sample_ids"][0], "OK1").status_code == 409

    def test_history_blocks_even_when_the_order_was_returned_to_draft(self, client, session, lab):
        reg = register(client, lab)
        make_report(session, lab, reg["order_id"], status=ReportStatus.DRAFT)
        set_status(session, reg["order_id"], OrderStatus.DIAGNOSIS)
        assert correct(client, lab, reg["sample_ids"][0], "OK1").status_code == 200
        add_event(session, lab, reg["order_id"], EventType.REPORT_CHANGES_REQUESTED)
        assert correct(client, lab, reg["sample_ids"][0], "OK2").status_code == 409

    def test_a_published_reports_issued_artifacts_keep_their_codes_because_the_order_is_blocked(self, client, session, lab):
        reg = register(client, lab)
        make_report(session, lab, reg["order_id"], status=ReportStatus.PUBLISHED)
        set_status(session, reg["order_id"], OrderStatus.CLOSED)
        assert correct(client, lab, reg["sample_ids"][0], "OK1").status_code == 409

    def test_other_sample_operations_are_untouched(self, client, session, lab):
        reg = register(client, lab)
        set_status(session, reg["order_id"], OrderStatus.REVIEW)
        sid = reg["sample_ids"][0]
        assert client.patch(f"{LAB}/samples/{sid}/notes", json={"notes": "n"}, headers=lab.headers).status_code == 200
        assert client.patch(f"{LAB}/samples/{sid}/state", json={"state": "PROCESSING"}, headers=lab.headers).status_code == 200


class TestRaces:
    def test_two_corrections_to_the_same_code_have_exactly_one_winner(self, fresh_client, session, lab):
        reg = register(fresh_client, lab, ("BX", "BX", "BX", "BX"))
        jobs = [
            (lambda c, sid=sid: c.post(f"{LAB}/samples/{sid}/code-correction",
                                       json={"sample_code": "SAME", "reason": "carrera"}, headers=lab.headers))
            for sid in reg["sample_ids"]
        ]
        results = run_parallel(jobs)
        statuses = sorted(r.status_code for r in results)
        assert statuses == [200, 409, 409, 409], [getattr(r, "text", r) for r in results]
        assert sample_codes(session, lab).count("SAME") == 1

    def test_a_correction_racing_automatic_creation_never_yields_a_duplicate(self, fresh_client, session, lab):
        reg = register(fresh_client, lab, ("BX",))
        target = "BX-2"  # exactly the code the generator is about to issue
        jobs = [lambda c: c.post(f"{LAB}/samples/{reg['sample_ids'][0]}/code-correction",
                                 json={"sample_code": target, "reason": "carrera"}, headers=lab.headers)]
        jobs += [lambda c: c.post(SAMPLES, json=lab.sample_payload(reg["order_id"]), headers=lab.headers) for _ in range(3)]
        results = run_parallel(jobs)
        assert all(not isinstance(r, Exception) and r.status_code == 200 for r in results[1:]), [getattr(r, "text", r) for r in results]
        codes = sample_codes(session, lab)
        assert len(codes) == len(set(codes)) == 4

    def test_a_correction_racing_a_review_submission_is_serialized_by_the_order_lock(self, engine, client, session, lab):
        """The submission takes the order lock first. While it holds it, a correction waits; once the
        submission commits its review evidence, the correction is refused."""
        import threading
        from sqlmodel import Session

        reg = register(client, lab)
        tenant_id, branch_id = str(lab.tenant.id), str(lab.branch.id)
        started, release = threading.Event(), threading.Event()
        outcome: dict = {}

        def submit():
            with Session(engine) as s:
                s.execute(text('SELECT 1 FROM "order" WHERE id = :i FOR NO KEY UPDATE'), {"i": reg["order_id"]})
                started.set()
                release.wait(10)
                s.execute(text(
                    "INSERT INTO order_event (id, tenant_id, branch_id, order_id, event_type, description, created_at) "
                    "VALUES (gen_random_uuid(), :t, :b, :o, 'REPORT_SUBMITTED', '', now())"),
                    {"t": tenant_id, "b": branch_id, "o": reg["order_id"]})
                s.commit()

        def attempt():
            from .numbering_support import new_client
            try:
                outcome["res"] = correct(new_client(), lab, reg["sample_ids"][0], "TARDE1")
                outcome["text"] = outcome["res"].text
            except Exception as exc:  # surfaced by the assertions below
                outcome["exc"] = repr(exc)

        from app.core.db import get_session
        from app.main import app

        def override():
            with Session(engine) as s:
                yield s
        app.dependency_overrides[get_session] = override
        t1 = threading.Thread(target=submit)
        t1.start()
        assert started.wait(10)
        t2 = threading.Thread(target=attempt)
        t2.start()
        t2.join(1.0)
        assert t2.is_alive(), f"the correction must wait for the order lock held by the submission: {outcome}"
        release.set()
        t1.join(10)
        t2.join(10)
        assert outcome["res"].status_code == 409
        session.expire_all()
        assert sample_detail(client, lab, reg["sample_ids"][0])["sample_code"] == reg["sample_codes"][0]
