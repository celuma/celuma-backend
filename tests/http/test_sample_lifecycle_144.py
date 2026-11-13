"""Céluma 1.4.4 hotfix (issue #73): sample actions around review and retraction, and withdrawing a report
from review. Real HTTP + PostgreSQL, synthetic data only.

1. A sample cannot be ADDED to an existing order while the order or its report is in active review (409
   `order_in_review`, decided under the order lock, nothing written).
2. After a formal retraction, sample edit (code correction, dates) and soft removal work again; order-level
   actions stay locked and issued artifacts are untouched.
3. "Retirar de revisión": the author of the current submission pulls an undecided review back to DRAFT.

Contract: `celuma-engineering/specs/laboratory/order-sample-management.md` and `specs/reports/lifecycle.md`.
"""
from __future__ import annotations

import threading
import time
import uuid

import pytest
from sqlalchemy import text

from app.models.audit import AuditLog
from app.models.enums import EventType, OrderStatus, ReportStatus, ReviewStatus, SampleState
from app.models.events import OrderEvent
from app.models.report import Report
from app.models.report_amendment import ReportAmendment

from .factories import (
    assign_reviewer,
    auth_headers,
    create_branch,
    create_order,
    create_report,
    create_sample,
    create_tenant,
    create_user,
)
from .management_support import LAB, count, decide_review, make_report, register, set_status
from .numbering_support import SAMPLES, Lab, counter_rows, freeze, fresh_client, ledger_rows  # noqa: F401
from .test_report_amendments import lab as amendment_lab  # noqa: F401  (fixture, re-exported under another name)

API = "/api/v1"


@pytest.fixture
def lab(session, monkeypatch):
    freeze(monkeypatch)
    return Lab(session, "Alfa", sample_types=("BX", "TJ"))


def add_sample(client, lab, order_id, key=None):
    headers = dict(lab.headers)
    if key:
        headers["Idempotency-Key"] = key
    return client.post(SAMPLES, json=lab.sample_payload(order_id), headers=headers)


def live_samples(session, order_id):
    return count(session, "SELECT count(*) FROM sample WHERE order_id = :i AND removed_at IS NULL", i=order_id)


def order_status(session, order_id):
    session.expire_all()
    return session.execute(text('SELECT status FROM "order" WHERE id = :i'), {"i": order_id}).scalar()


def hold_order_lock_then(engine, order_id, action, delay=0.6):
    """Hold the order lifecycle lock in one transaction, run `action` (a callable that blocks on that lock) in a
    thread, then run the holder's write and commit. Returns the thread's result."""
    holder = engine.connect()
    tx = holder.begin()
    holder.execute(text('SELECT 1 FROM "order" WHERE id = :i FOR NO KEY UPDATE'), {"i": order_id})
    out = {}

    def run():
        out["result"] = action()

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(delay)  # the request is now queued on the lock
    return holder, tx, thread, out


# ===================================================================== 1. sample creation under review


class TestSampleCreationGuard:
    def test_the_ordinary_eligible_order_still_accepts_samples(self, client, session, lab):
        reg = register(client, lab)
        assert add_sample(client, lab, reg["order_id"]).status_code == 200
        make_report(session, lab, reg["order_id"])  # a plain DRAFT report is not a review
        assert add_sample(client, lab, reg["order_id"]).status_code == 200
        assert live_samples(session, reg["order_id"]) == 3

    @pytest.mark.parametrize("scenario", ["report_in_review", "report_approved", "order_in_review_status"])
    def test_active_review_refuses_a_new_sample_and_changes_nothing(self, client, session, lab, scenario):
        reg = register(client, lab)
        oid = reg["order_id"]
        if scenario == "report_in_review":
            make_report(session, lab, oid, status=ReportStatus.IN_REVIEW)
        elif scenario == "report_approved":
            make_report(session, lab, oid, status=ReportStatus.APPROVED)
        else:
            set_status(session, oid, OrderStatus.REVIEW)
        before = (live_samples(session, oid), order_status(session, oid), counter_rows(session), ledger_rows(session),
                  count(session, "SELECT count(*) FROM order_event WHERE order_id = :i", i=oid))
        res = add_sample(client, lab, oid)
        assert res.status_code == 409, res.text
        assert res.json()["detail"]["code"] == "order_in_review"
        assert res.json()["detail"]["blockers"]
        session.expire_all()
        after = (live_samples(session, oid), order_status(session, oid), counter_rows(session), ledger_rows(session),
                 count(session, "SELECT count(*) FROM order_event WHERE order_id = :i", i=oid))
        assert after == before

    def test_a_refused_request_can_be_retried_with_the_same_key_once_the_review_ends(self, client, session, lab):
        reg = register(client, lab)
        oid = reg["order_id"]
        report, _ = make_report(session, lab, oid, status=ReportStatus.IN_REVIEW)
        key = f"k-{uuid.uuid4().hex}"
        assert add_sample(client, lab, oid, key).status_code == 409
        session.execute(text("UPDATE report SET status = 'DRAFT' WHERE id = :i"), {"i": str(report.id)})
        session.commit()
        ok = add_sample(client, lab, oid, key)
        assert ok.status_code == 200, ok.text
        assert live_samples(session, oid) == 2

    def test_a_valid_replay_still_returns_the_original_sample_during_review(self, client, session, lab):
        reg = register(client, lab)
        oid = reg["order_id"]
        key = f"k-{uuid.uuid4().hex}"
        first = add_sample(client, lab, oid, key)
        assert first.status_code == 200
        make_report(session, lab, oid, status=ReportStatus.IN_REVIEW)
        replay = add_sample(client, lab, oid, key)
        assert replay.status_code == 200
        assert replay.json()["id"] == first.json()["id"]
        assert live_samples(session, oid) == 2

    def test_unified_registration_of_a_new_order_is_unaffected(self, client, session, lab):
        busy = register(client, lab)
        make_report(session, lab, busy["order_id"], status=ReportStatus.IN_REVIEW)
        fresh = register(client, lab, ("BX", "TJ"))
        assert len(fresh["sample_ids"]) == 2

    def test_the_management_state_reflects_the_server_rule(self, client, session, lab):
        reg = register(client, lab)
        url = f"{LAB}/orders/{reg['order_id']}/management"
        state = client.get(url, headers=lab.headers).json()
        assert state["sample_creation"] == {"permitted": True, "allowed": True, "blockers": []}
        make_report(session, lab, reg["order_id"], status=ReportStatus.IN_REVIEW)
        state = client.get(url, headers=lab.headers).json()
        assert state["sample_creation"]["allowed"] is False
        assert state["sample_creation"]["blockers"][0]["code"] == "order_in_review"
        billing = client.get(url, headers=lab.headers_for("billing")).json()
        assert billing["sample_creation"]["permitted"] is False

    def test_a_role_without_the_capability_cannot_create_even_when_eligible(self, client, session, lab):
        reg = register(client, lab)
        res = client.post(SAMPLES, json=lab.sample_payload(reg["order_id"]), headers=lab.headers_for("billing"))
        assert res.status_code == 403

    def test_submission_racing_creation_is_serialised_by_the_order_lock(self, fresh_client, engine, session, lab):
        reg = register(fresh_client, lab)
        oid = reg["order_id"]
        report, _ = make_report(session, lab, oid)
        # A submission holds the order lock and is about to commit IN_REVIEW; the creation queues behind it.
        holder, tx, thread, out = hold_order_lock_then(
            engine, oid, lambda: add_sample(fresh_client, lab, oid))
        holder.execute(text("UPDATE report SET status = 'IN_REVIEW' WHERE id = :i"), {"i": str(report.id)})
        tx.commit()
        holder.close()
        thread.join(timeout=60)
        assert out["result"].status_code == 409, out["result"].text
        assert live_samples(session, oid) == 1


# ===================================================================== 2. retraction exception


def retract(client, lab, order_id, *, with_samples=2):
    """A published, signed, PDF-bearing report retracted through the REAL route (order in Diagnosis)."""
    session = lab.session
    session.execute(text("UPDATE sample SET state = 'PROCESSING' WHERE order_id = :i"), {"i": order_id})
    report, version = make_report(session, lab, order_id, status=ReportStatus.PUBLISHED)
    session.execute(text("UPDATE report SET published_at = now() WHERE id = :i"), {"i": str(report.id)})
    session.execute(
        text("UPDATE report_version SET signed_at = now(), signed_by = :u, pdf_sha256 = :h WHERE report_id = :r"),
        {"u": str(lab.user.id), "h": "a" * 64, "r": str(report.id)})
    decide_review(session, lab, order_id, ReviewStatus.APPROVED)
    for event_type in (EventType.REPORT_SUBMITTED, EventType.REPORT_APPROVED, EventType.REPORT_PUBLISHED):
        session.add(OrderEvent(
            tenant_id=lab.tenant.id, branch_id=lab.branch.id, order_id=order_id, event_type=event_type,
            description="", event_metadata={"report_id": str(report.id)}, created_by=lab.user.id))
    session.commit()
    set_status(session, order_id, OrderStatus.CLOSED)
    res = client.post(f"{API}/reports/{report.id}/retract", json={"changelog": "motivo sintético"}, headers=lab.headers)
    assert res.status_code == 200, res.text
    session.expire_all()
    return report, version


def artifact_snapshot(session, report_id):
    session.expire_all()
    rows = session.execute(text(
        "SELECT r.status, r.published_at, v.id, v.signed_at, v.signed_by, v.pdf_sha256, v.json_storage_id "
        "FROM report r JOIN report_version v ON v.report_id = r.id WHERE r.id = :r ORDER BY v.version_no"),
        {"r": str(report_id)}).all()
    reviews = session.execute(text(
        "SELECT status, decision_at FROM report_review WHERE report_id IS NOT DISTINCT FROM report_id")).all()
    events = session.execute(text(
        "SELECT event_type, count(*) FROM order_event WHERE event_type::text LIKE 'REPORT_%' "
        "GROUP BY event_type ORDER BY event_type")).all()
    return [tuple(r) for r in rows], [tuple(r) for r in reviews], [tuple(r) for r in events]


class TestRetractedReportSampleActions:
    def test_retraction_returns_the_order_to_diagnosis(self, client, session, lab):
        reg = register(client, lab, ("BX", "TJ"))
        retract(client, lab, reg["order_id"])
        assert order_status(session, reg["order_id"]) == "DIAGNOSIS"

    def test_edit_and_soft_removal_work_and_issued_artifacts_are_untouched(self, client, session, lab):
        reg = register(client, lab, ("BX", "TJ", "BX"))
        oid, (s1, s2, s3) = reg["order_id"], reg["sample_ids"]
        report, _ = retract(client, lab, oid)
        before = artifact_snapshot(session, report.id)

        state = client.get(f"{LAB}/samples/{s1}/management", headers=lab.headers).json()
        assert state["correction"]["allowed"] and state["dates"]["allowed"] and state["removal"]["allowed"], state

        fixed = client.post(f"{LAB}/samples/{s1}/code-correction",
                            json={"sample_code": "SYN-001", "reason": "captura sintética"}, headers=lab.headers)
        assert fixed.status_code == 200, fixed.text
        dated = client.patch(f"{LAB}/samples/{s2}/dates",
                             json={"collected_at": "2026-10-10T09:00:00", "reason": "fecha sintética"}, headers=lab.headers)
        assert dated.status_code == 200, dated.text
        removed = client.post(f"{LAB}/samples/{s3}/removal", json={"reason": "muestra duplicada sintética"}, headers=lab.headers)
        assert removed.status_code == 200, removed.text

        # soft removal: tombstone, not a delete; audit names the exception; order recalculated, not REVIEW
        assert count(session, "SELECT count(*) FROM sample WHERE id = :i AND removed_at IS NOT NULL", i=s3) == 1
        session.expire_all()
        from sqlmodel import select
        rows = session.exec(select(AuditLog).where(AuditLog.action.in_(
            ["SAMPLE.REMOVED", "SAMPLE.CODE_CORRECTED", "SAMPLE.DATES_UPDATED"]))).all()
        assert len(rows) == 3
        assert all(str(report.id) in (r.event_metadata or {}).get("retraction_exception_report_ids", []) for r in rows)
        assert order_status(session, oid) == "DIAGNOSIS"
        # the retracted issuance is exactly as it was (only sample-level rows were added)
        assert artifact_snapshot(session, report.id) == before

    def test_order_level_actions_stay_locked(self, client, session, lab):
        reg = register(client, lab, ("BX", "TJ"))
        oid = reg["order_id"]
        retract(client, lab, oid)
        assert client.patch(f"{LAB}/orders/{oid}", json={"expediente": "1"}, headers=lab.headers).status_code == 409
        assert client.patch(f"{LAB}/orders/{oid}/notes", json={"notes": "x"}, headers=lab.headers).status_code == 409
        removal = client.post(f"{LAB}/orders/{oid}/removal", json={"reason": "intento sintético de retiro"}, headers=lab.headers)
        assert removal.status_code == 409
        state = client.get(f"{LAB}/orders/{oid}/management", headers=lab.headers).json()
        assert state["edit"]["allowed"] is False and state["removal"]["allowed"] is False

    def test_existing_sample_restrictions_are_kept(self, client, session, lab):
        reg = register(client, lab)  # a single sample
        retract(client, lab, reg["order_id"])
        res = client.post(f"{LAB}/samples/{reg['sample_ids'][0]}/removal", json={"reason": "captura duplicada sintética"}, headers=lab.headers)
        assert res.status_code == 409
        assert "last_sample" in {b["code"] for b in res.json()["detail"]["blockers"]}

    def test_unauthorized_and_cross_tenant_callers_are_refused(self, client, session, lab):
        reg = register(client, lab, ("BX", "TJ"))
        retract(client, lab, reg["order_id"])
        sid = reg["sample_ids"][0]
        billing = lab.headers_for("billing")
        assert client.post(f"{LAB}/samples/{sid}/removal", json={"reason": "captura duplicada sintética"}, headers=billing).status_code == 403
        assert client.patch(f"{LAB}/samples/{sid}/dates", json={"collected_at": None, "reason": "captura duplicada sintética"},
                            headers=billing).status_code == 403
        other = Lab(session, "Beta")
        assert client.post(f"{LAB}/samples/{sid}/removal", json={"reason": "captura duplicada sintética"}, headers=other.headers).status_code == 404
        assert client.get(f"{LAB}/samples/{sid}/management", headers=other.headers).status_code == 404
        assert count(session, "SELECT count(*) FROM sample WHERE id = :i AND removed_at IS NOT NULL", i=sid) == 0

    def test_another_current_report_still_blocks(self, client, session, lab):
        reg = register(client, lab, ("BX", "TJ"))
        oid = reg["order_id"]
        retract(client, lab, oid)
        other, _ = make_report(session, lab, oid, status=ReportStatus.IN_REVIEW)  # a different, live report
        state = client.get(f"{LAB}/samples/{reg['sample_ids'][0]}/management", headers=lab.headers).json()
        codes = {b["code"] for b in state["removal"]["blockers"]}
        assert state["removal"]["allowed"] is False and "report_not_draft" in codes
        # and the unattributed evidence of the retracted cycle is not waived while another report exists
        res = client.post(f"{LAB}/samples/{reg['sample_ids'][0]}/removal", json={"reason": "captura duplicada sintética"}, headers=lab.headers)
        assert res.status_code == 409

    def test_an_active_signing_or_publication_operation_blocks(self, client, session, lab):
        reg = register(client, lab, ("BX", "TJ"))
        oid = reg["order_id"]
        report, _ = retract(client, lab, oid)
        session.execute(text("UPDATE report_version SET publish_started_at = now(), publish_started_by = :u WHERE report_id = :r"),
                        {"u": str(lab.user.id), "r": str(report.id)})
        session.commit()
        res = client.post(f"{LAB}/samples/{reg['sample_ids'][0]}/removal", json={"reason": "captura duplicada sintética"}, headers=lab.headers)
        assert res.status_code == 409
        assert "report_operation_active" in {b["code"] for b in res.json()["detail"]["blockers"]}

    def test_an_open_amendment_blocks(self, client, session, lab):
        reg = register(client, lab, ("BX", "TJ"))
        oid = reg["order_id"]
        report, version = retract(client, lab, oid)
        session.add(ReportAmendment(
            tenant_id=lab.tenant.id, report_id=report.id, amendment_no=1, state="OPEN", category="CORRECTION",
            reason="sintética", source_version_id=version.id, requested_by=lab.user.id))
        session.commit()
        res = client.post(f"{LAB}/samples/{reg['sample_ids'][0]}/removal", json={"reason": "captura duplicada sintética"}, headers=lab.headers)
        assert res.status_code == 409
        assert "report_amendment_open" in {b["code"] for b in res.json()["detail"]["blockers"]}

    def test_a_non_retracted_published_report_is_still_fully_locked(self, client, session, lab):
        reg = register(client, lab, ("BX", "TJ"))
        oid = reg["order_id"]
        report, _ = make_report(session, lab, oid, status=ReportStatus.PUBLISHED)
        session.execute(text("UPDATE report SET published_at = now() WHERE id = :i"), {"i": str(report.id)})
        session.commit()
        set_status(session, oid, OrderStatus.DIAGNOSIS)
        res = client.post(f"{LAB}/samples/{reg['sample_ids'][0]}/removal", json={"reason": "captura duplicada sintética"}, headers=lab.headers)
        assert res.status_code == 409

    def test_the_order_phase_window_still_applies(self, client, session, lab):
        reg = register(client, lab, ("BX", "TJ"))
        retract(client, lab, reg["order_id"])
        set_status(session, reg["order_id"], OrderStatus.CLOSED)
        res = client.post(f"{LAB}/samples/{reg['sample_ids'][0]}/removal", json={"reason": "captura duplicada sintética"}, headers=lab.headers)
        assert res.status_code == 409
        assert "order_phase" in {b["code"] for b in res.json()["detail"]["blockers"]}


# ===================================================================== 3. withdraw from review


@pytest.fixture
def world(session, client):
    """One tenant: an order with a ready sample, a DRAFT V2 report authored by `author`, a pending reviewer."""
    tenant = create_tenant(session)
    branch = create_branch(session, tenant)
    order = create_order(session, tenant, branch, order_code="ORD-WD-1")
    create_sample(session, tenant, branch, order, state=SampleState.READY)
    author = create_user(session, tenant, email="author@wd.example", roles=("pathologist",))
    other = create_user(session, tenant, email="other@wd.example", roles=("pathologist",))
    reviewer = create_user(session, tenant, email="rev@wd.example", roles=("reviewer",))
    admin = create_user(session, tenant, email="admin@wd.example", roles=("admin",))
    tech = create_user(session, tenant, email="tech@wd.example", roles=("lab_tech",))
    report, version = create_report(session, tenant, branch, order, created_by=author, authored_by=author)
    review = assign_reviewer(session, order, reviewer, report=report)
    return {"tenant": tenant, "branch": branch, "order": order, "report": report, "version": version,
            "author": author, "other": other, "reviewer": reviewer, "admin": admin, "tech": tech, "review": review}


def w_submit(client, world, actor=None):
    return client.post(f"{API}/reports/{world['report'].id}/submit", json={}, headers=auth_headers(actor or world["author"]))


def w_withdraw(client, world, submission_id, actor=None, report=None):
    return client.post(f"{API}/reports/{(report or world['report']).id}/withdraw-review",
                       json={"submission_id": submission_id}, headers=auth_headers(actor or world["author"]))


def w_state(client, world, actor=None):
    res = client.get(f"{API}/reports/{world['report'].id}/full", headers=auth_headers(actor or world["author"]))
    assert res.status_code == 200, res.text
    return res.json()["review_withdrawal"]


def w_save(client, world, actor=None):
    body = {"base": {}, "sections": {}, "base_order": [], "section_order": []}
    return client.post(
        f"{API}/reports/{world['report'].id}/new_version",
        json={"tenant_id": str(world["tenant"].id), "branch_id": str(world["branch"].id),
              "order_id": str(world["order"].id), "title": "Reporte", "report": body,
              "created_by": str((actor or world["author"]).id)},
        headers=auth_headers(actor or world["author"]))


def report_status(session, report):
    session.expire_all()
    return session.get(Report, report.id).status


@pytest.mark.parametrize("exit_route", ["withdraw", "request-changes", "worklist"])
def test_received_samples_can_be_corrected_between_reviews(client, session, world, exit_route):
    order = world["order"]
    report = world["report"]
    session.execute(text("UPDATE sample SET state = 'RECEIVED' WHERE order_id = :id"), {"id": str(order.id)})
    sample = create_sample(session, world["tenant"], world["branch"], order, sample_code="S-2")
    headers = auth_headers(world["admin"])
    sample_url = f"{LAB}/samples/{sample.id}"

    assert w_submit(client, world).status_code == 200
    assert order_status(session, str(order.id)) == "REVIEW"
    assert not client.get(f"{sample_url}/management", headers=headers).json()["dates"]["allowed"]
    reviewer_headers = auth_headers(world["reviewer"])
    decision_url = f"{API}/report-reviews/{world['review'].id}/decision"
    if exit_route == "withdraw":
        result = w_withdraw(client, world, w_state(client, world)["submission_id"])
        assert client.post(decision_url, json={"decision": "approved"}, headers=reviewer_headers).status_code == 409
    elif exit_route == "worklist":
        result = client.post(decision_url, json={"decision": "rejected"}, headers=reviewer_headers)
    else:
        result = client.post(
            f"{API}/reports/{report.id}/request-changes", json={"comment": "Corregir muestra"}, headers=reviewer_headers,
        )
    assert result.status_code == 200, result.text
    assert report_status(session, report) == ReportStatus.DRAFT
    assert order_status(session, str(order.id)) == "DIAGNOSIS"

    state = client.get(f"{sample_url}/management", headers=headers).json()
    assert all(state[action]["allowed"] for action in ("correction", "dates", "removal")), state
    edit = client.post(
        f"{sample_url}/code-correction", json={"sample_code": "SYN-2", "reason": "Corrección sintética"}, headers=headers,
    )
    assert edit.status_code == 200, edit.text
    removal = client.post(f"{sample_url}/removal", json={"reason": "Captura duplicada"}, headers=headers)
    assert removal.status_code == 200, removal.text

    assert w_save(client, world).status_code == 200
    assert w_submit(client, world).status_code == 200
    assert order_status(session, str(order.id)) == "REVIEW"
    # The order detail timeline uses this payload, including for orders
    # already left in RECEIVED by the previous build.
    session.execute(text('UPDATE "order" SET status = :status WHERE id = :id'), {"id": str(order.id), "status": "RECEIVED"})
    session.commit()
    detail = client.get(f"{LAB}/orders/{order.id}/full", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["order"]["status"] == "REVIEW"


class TestWithdrawFromReview:
    def test_the_submitter_withdraws_corrects_and_resubmits_without_waiting(self, client, session, world):
        assert w_state(client, world)["allowed"] is False  # a draft cannot be withdrawn
        assert w_submit(client, world).status_code == 200
        assert report_status(session, world["report"]) == ReportStatus.IN_REVIEW
        state = w_state(client, world)
        assert state["allowed"] is True and state["submission_id"]

        versions_before = count(session, "SELECT count(*) FROM report_version WHERE report_id = :r", r=str(world["report"].id))
        reviews_before = session.execute(text("SELECT id, reviewer_user_id FROM report_review ORDER BY id")).all()
        res = w_withdraw(client, world, state["submission_id"])
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "DRAFT"
        assert report_status(session, world["report"]) == ReportStatus.DRAFT
        assert order_status(session, str(world["order"].id)) in ("DIAGNOSIS", "PROCESSING")  # no longer REVIEW
        # preserved: versions and content, assignments, earlier audit/timeline
        assert count(session, "SELECT count(*) FROM report_version WHERE report_id = :r", r=str(world["report"].id)) == versions_before
        assert session.execute(text("SELECT id, reviewer_user_id FROM report_review ORDER BY id")).all() == reviews_before
        assert count(session, "SELECT count(*) FROM audit_log WHERE action = 'REPORT.SUBMIT'") == 1
        assert count(session, "SELECT count(*) FROM order_event WHERE event_type = 'REPORT_SUBMITTED'") == 1
        # recorded exactly once in audit and timeline
        assert count(session, "SELECT count(*) FROM audit_log WHERE action = 'REPORT.WITHDRAW_REVIEW'") == 1
        events = session.execute(text(
            "SELECT event_metadata FROM order_event WHERE event_type = 'STATUS_CHANGED'")).all()
        assert len(events) == 1 and events[0][0]["action"] == "REPORT_REVIEW_WITHDRAWN"
        assert events[0][0]["submission_id"] == state["submission_id"]

        # correct and resubmit immediately, no reviewer involved
        assert w_save(client, world).status_code == 200
        again = w_submit(client, world)
        assert again.status_code == 200, again.text
        assert report_status(session, world["report"]) == ReportStatus.IN_REVIEW
        assert w_state(client, world)["submission_id"] != state["submission_id"]

    def test_a_different_submit_capable_user_cannot_cancel_it(self, client, session, world):
        assert w_submit(client, world).status_code == 200
        sid = w_state(client, world)["submission_id"]
        for actor in (world["other"], world["admin"]):
            shown = w_state(client, world, actor)
            assert shown["allowed"] is False
        res = w_withdraw(client, world, sid, actor=world["other"])
        assert res.status_code == 403, res.text
        assert res.json()["detail"]["code"] == "review_withdraw_not_submitter"
        assert report_status(session, world["report"]) == ReportStatus.IN_REVIEW

    def test_a_role_without_reports_submit_is_refused(self, client, session, world):
        assert w_submit(client, world).status_code == 200
        sid = w_state(client, world)["submission_id"]
        assert w_withdraw(client, world, sid, actor=world["tech"]).status_code == 403
        assert report_status(session, world["report"]) == ReportStatus.IN_REVIEW

    def test_another_tenant_is_refused_and_nothing_changes(self, client, session, world):
        assert w_submit(client, world).status_code == 200
        sid = w_state(client, world)["submission_id"]
        foreign_tenant = create_tenant(session, name="Otro laboratorio")
        intruder = create_user(session, foreign_tenant, email="x@otro.example", roles=("pathologist",))
        res = w_withdraw(client, world, sid, actor=intruder)
        assert res.status_code == 403
        assert report_status(session, world["report"]) == ReportStatus.IN_REVIEW

    def test_a_stale_request_for_an_older_submission_is_refused(self, client, session, world):
        assert w_submit(client, world).status_code == 200
        old = w_state(client, world)["submission_id"]
        assert w_withdraw(client, world, old).status_code == 200
        assert w_submit(client, world).status_code == 200
        res = w_withdraw(client, world, old)
        assert res.status_code == 409
        assert res.json()["detail"]["code"] == "review_submission_stale"
        assert report_status(session, world["report"]) == ReportStatus.IN_REVIEW
        # the fresh id works
        assert w_withdraw(client, world, w_state(client, world)["submission_id"]).status_code == 200

    @pytest.mark.parametrize("decision", ["approve", "request_changes"])
    def test_a_decided_review_cannot_be_withdrawn(self, client, session, world, decision):
        assert w_submit(client, world).status_code == 200
        sid = w_state(client, world)["submission_id"]
        route = "approve" if decision == "approve" else "request-changes"
        body = {} if decision == "approve" else {"comment": "ajustar"}
        done = client.post(f"{API}/reports/{world['report'].id}/{route}", json=body, headers=auth_headers(world["reviewer"]))
        assert done.status_code == 200, done.text
        decided = report_status(session, world["report"])
        res = w_withdraw(client, world, sid)
        assert res.status_code == 409 and res.json()["detail"]["code"] == "review_withdraw_not_pending"
        assert report_status(session, world["report"]) == decided
        assert w_state(client, world)["allowed"] is False

    def test_a_decision_that_wins_the_race_wins(self, fresh_client, engine, session, world):
        assert w_submit(fresh_client, world).status_code == 200
        sid = w_state(fresh_client, world)["submission_id"]
        oid = str(world["order"].id)
        holder, tx, thread, out = hold_order_lock_then(engine, oid, lambda: w_withdraw(fresh_client, world, sid))
        holder.execute(text("UPDATE report SET status = 'APPROVED' WHERE id = :i"), {"i": str(world["report"].id)})
        holder.execute(text("UPDATE report_review SET status = 'APPROVED', decision_at = now()"))
        tx.commit()
        holder.close()
        thread.join(timeout=60)
        assert out["result"].status_code == 409, out["result"].text
        assert report_status(session, world["report"]) == ReportStatus.APPROVED

    def test_a_decision_recorded_without_a_status_change_still_blocks(self, client, session, world):
        assert w_submit(client, world).status_code == 200
        sid = w_state(client, world)["submission_id"]
        session.execute(text("UPDATE report_review SET status = 'REJECTED', decision_at = now()"))
        session.commit()
        res = w_withdraw(client, world, sid)
        assert res.status_code == 409 and res.json()["detail"]["code"] == "review_decision_recorded"

    def test_a_report_with_no_recorded_submission_cannot_be_withdrawn(self, client, session, world):
        session.execute(text("UPDATE report SET status = 'IN_REVIEW'"))
        session.commit()
        res = w_withdraw(client, world, str(uuid.uuid4()))
        assert res.status_code == 409 and res.json()["detail"]["code"] == "review_submission_unknown"

    def test_only_an_in_review_report_can_be_withdrawn(self, client, session, world):
        res = w_withdraw(client, world, str(uuid.uuid4()))
        assert res.status_code == 409 and res.json()["detail"]["code"] == "review_withdraw_not_pending"


class TestWithdrawDuringAnAmendment:
    def test_the_amendment_and_the_delivered_emission_are_untouched(self, client, session, amendment_lab):
        from .test_report_amendments import _open, _save, _submit
        lab = amendment_lab
        report_id = str(lab["report"].id)
        assert _open(client, lab).status_code == 200
        assert _save(client, lab).status_code == 200
        assert _submit(client, lab).status_code == 200
        session.expire_all()
        assert session.get(Report, lab["report"].id).status == ReportStatus.IN_REVIEW

        delivered = session.execute(text(
            "SELECT id, signed_at, pdf_sha256, json_storage_id FROM report_version WHERE report_id = :r AND signed_at IS NOT NULL "
            "ORDER BY version_no"), {"r": report_id}).all()
        amendments = session.execute(text("SELECT id, state, source_version_id FROM report_amendment")).all()
        assert amendments and amendments[0][1] == "OPEN"

        full = client.get(f"{API}/reports/{report_id}/full", headers=auth_headers(lab["author"])).json()
        assert full["review_withdrawal"]["allowed"] is True
        res = client.post(f"{API}/reports/{report_id}/withdraw-review",
                          json={"submission_id": full["review_withdrawal"]["submission_id"]},
                          headers=auth_headers(lab["author"]))
        assert res.status_code == 200, res.text

        session.expire_all()
        assert session.get(Report, lab["report"].id).status == ReportStatus.DRAFT
        assert session.execute(text("SELECT id, state, source_version_id FROM report_amendment")).all() == amendments
        assert session.execute(text(
            "SELECT id, signed_at, pdf_sha256, json_storage_id FROM report_version WHERE report_id = :r AND signed_at IS NOT NULL "
            "ORDER BY version_no"), {"r": report_id}).all() == delivered
        event = session.execute(text(
            "SELECT event_metadata FROM order_event WHERE event_type = 'STATUS_CHANGED' "
            "AND event_metadata->>'action' = 'REPORT_REVIEW_WITHDRAWN'")).scalar()
        assert event and event.get("amendment_no") == 1  # correlated with the open amendment
        # the author corrects and resubmits within the same amendment
        assert _save(client, lab, note="Nota de enmienda corregida y suficiente.").status_code == 200
        assert _submit(client, lab).status_code == 200
