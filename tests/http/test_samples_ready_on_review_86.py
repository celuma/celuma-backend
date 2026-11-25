"""Céluma issue #86 (PROPOSED, pending Rafael's review): a successful report submission that puts its order in
REVIEW marks the order's eligible samples READY ("Lista") in the same transaction.

Proposed rule: active samples of the same order and tenant in RECEIVED or PROCESSING become READY; READY, DAMAGED
("Insuficiente"), CANCELLED and removed samples are preserved; a return to draft never reverts a sample; the same
rule applies to resubmission and to the submission of an open amendment. Real HTTP + PostgreSQL, synthetic data.

Contract: `celuma-engineering/specs/laboratory/sample-status.md` and ADR 0024.
"""
from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import text

from app.models.enums import OrderStatus, SampleState
from app.models.notification import NotificationType

from .factories import (
    assign_reviewer,
    assign_to_order,
    auth_headers,
    create_branch,
    create_order,
    create_report,
    create_sample,
    create_tenant,
    create_user,
    notifications_for,
)
from .numbering_support import fresh_client  # noqa: F401  (fixture)
from .test_report_amendments import lab as amendment_lab  # noqa: F401  (fixture, re-exported under another name)

API = "/api/v1"
LAB = f"{API}/laboratory"


# --------------------------------------------------------------------------- fixtures and helpers


@pytest.fixture
def world(session):
    """One tenant, an order with one sample per interesting state, a DRAFT V2 report and a pending reviewer;
    plus a sibling order of the same tenant and an order of a foreign tenant, each with a RECEIVED sample."""
    tenant = create_tenant(session)
    branch = create_branch(session, tenant)
    order = create_order(session, tenant, branch, order_code="ORD-86-1")
    samples = {
        "received": create_sample(session, tenant, branch, order, sample_code="S-REC"),
        "processing": create_sample(session, tenant, branch, order, sample_code="S-PRO", state=SampleState.PROCESSING),
        "ready": create_sample(session, tenant, branch, order, sample_code="S-RDY", state=SampleState.READY),
        "damaged": create_sample(session, tenant, branch, order, sample_code="S-DAM", state=SampleState.DAMAGED),
        "cancelled": create_sample(session, tenant, branch, order, sample_code="S-CAN", state=SampleState.CANCELLED),
        "removed": create_sample(session, tenant, branch, order, sample_code="S-RET", state=SampleState.PROCESSING),
    }
    author = create_user(session, tenant, email="author@86.example", roles=("pathologist",))
    reviewer = create_user(session, tenant, email="rev@86.example", roles=("reviewer",))
    tech = create_user(session, tenant, email="tech@86.example", roles=("lab_tech",))
    admin = create_user(session, tenant, email="admin@86.example", roles=("admin",))
    assign_to_order(session, tenant, order, tech)
    report, version = create_report(session, tenant, branch, order, created_by=author, authored_by=author)
    review = assign_reviewer(session, order, reviewer, report=report)
    # A tombstoned ("retirada") sample: hidden from ordinary queries, never touched by the transition.
    session.execute(
        text("UPDATE sample SET removed_at = now(), removed_by = :u, removal_reason = 'Captura duplicada' WHERE id = :i"),
        {"u": str(admin.id), "i": str(samples["removed"].id)},
    )

    sibling = create_order(session, tenant, branch, order_code="ORD-86-2")
    sibling_sample = create_sample(session, tenant, branch, sibling, sample_code="S-SIB")

    foreign_tenant = create_tenant(session, name="Foreign")
    foreign_branch = create_branch(session, foreign_tenant)
    foreign_order = create_order(session, foreign_tenant, foreign_branch, order_code="ORD-86-F")
    foreign_sample = create_sample(session, foreign_tenant, foreign_branch, foreign_order, sample_code="S-FOR")
    session.commit()
    return {
        "tenant": tenant, "branch": branch, "order": order, "samples": samples, "report": report, "version": version,
        "author": author, "reviewer": reviewer, "tech": tech, "admin": admin, "review": review,
        "sibling_sample": sibling_sample, "foreign_sample": foreign_sample,
    }


def submit(client, world, actor=None):
    return client.post(f"{API}/reports/{world['report'].id}/submit", json={}, headers=auth_headers(actor or world["author"]))


def save(client, world):
    body = {"base": {}, "sections": {}, "base_order": [], "section_order": []}
    return client.post(
        f"{API}/reports/{world['report'].id}/new_version",
        json={"tenant_id": str(world["tenant"].id), "branch_id": str(world["branch"].id),
              "order_id": str(world["order"].id), "title": "Reporte", "report": body,
              "created_by": str(world["author"].id)},
        headers=auth_headers(world["author"]))


def withdraw(client, world):
    full = client.get(f"{API}/reports/{world['report'].id}/full", headers=auth_headers(world["author"])).json()
    return client.post(f"{API}/reports/{world['report'].id}/withdraw-review",
                       json={"submission_id": full["review_withdrawal"]["submission_id"]},
                       headers=auth_headers(world["author"]))


def states(conn_or_session, *samples):
    """Committed state of each sample, read through raw SQL (bypasses the identity map and the removed filter)."""
    if hasattr(conn_or_session, "expire_all"):
        conn_or_session.expire_all()
    rows = dict(conn_or_session.execute(
        text("SELECT id::text, state::text FROM sample WHERE id::text = ANY(:ids)"),
        {"ids": [str(s.id) for s in samples]}).all())
    return [rows[str(s.id)] for s in samples]


def auto_events(conn_or_session, order_id=None):
    """SAMPLE_STATE_CHANGED rows written by this rule, oldest first."""
    sql = ("SELECT sample_id::text, event_metadata, created_by::text FROM order_event "
           "WHERE event_type = 'SAMPLE_STATE_CHANGED' AND event_metadata->>'trigger' = 'report_submitted'")
    params = {}
    if order_id is not None:
        sql += " AND order_id = :o"
        params["o"] = str(order_id)
    return conn_or_session.execute(text(sql + " ORDER BY created_at, id"), params).all()


def submissions(conn_or_session, order_id):
    return conn_or_session.execute(text(
        "SELECT id::text FROM order_event WHERE event_type = 'REPORT_SUBMITTED' AND order_id = :o ORDER BY created_at, id"),
        {"o": str(order_id)}).scalars().all()


def submit_audits(conn_or_session, report_id):
    return conn_or_session.execute(text(
        "SELECT new_values FROM audit_log WHERE action = 'REPORT.SUBMIT' AND entity_id = :r ORDER BY created_at, id"),
        {"r": str(report_id)}).scalars().all()


def order_status(session, order_id):
    session.expire_all()
    return session.execute(text('SELECT status::text FROM "order" WHERE id = :i'), {"i": str(order_id)}).scalar()


def all_states(session, world):
    s = world["samples"]
    return states(session, s["received"], s["processing"], s["ready"], s["damaged"], s["cancelled"], s["removed"],
                  world["sibling_sample"], world["foreign_sample"])


INITIAL = ["RECEIVED", "PROCESSING", "READY", "DAMAGED", "CANCELLED", "PROCESSING", "RECEIVED", "RECEIVED"]
AFTER = ["READY", "READY", "READY", "DAMAGED", "CANCELLED", "PROCESSING", "RECEIVED", "RECEIVED"]


# --------------------------------------------------------------------------- the transition


def test_mixed_states_only_received_and_processing_become_ready(client, session, world):
    assert all_states(session, world) == INITIAL
    res = submit(client, world)
    assert res.status_code == 200, res.text
    assert order_status(session, world["order"].id) == OrderStatus.REVIEW.value
    # Same order and tenant only; READY/DAMAGED/CANCELLED/removed preserved; sibling and foreign orders untouched.
    assert all_states(session, world) == AFTER

    events = auto_events(session)
    s = world["samples"]
    assert {(e[0], e[1]["old_state"]) for e in events} == {
        (str(s["received"].id), "RECEIVED"), (str(s["processing"].id), "PROCESSING")}
    [submission_id] = submissions(session, world["order"].id)
    for _, meta, created_by in events:
        assert meta["new_state"] == "READY"
        assert meta["report_id"] == str(world["report"].id)
        assert meta["submission_id"] == submission_id
        assert meta["sample_code"] in {"S-REC", "S-PRO"}
        assert created_by == str(world["author"].id)
        assert "amendment_no" not in meta

    [audit] = submit_audits(session, world["report"].id)
    assert audit["status"] == "IN_REVIEW"
    assert sorted((x["sample_code"], x["old_state"]) for x in audit["samples_marked_ready"]) == [
        ("S-PRO", "PROCESSING"), ("S-REC", "RECEIVED")]


def test_assignees_get_one_in_app_notification_per_changed_sample(client, session, world):
    assert submit(client, world).status_code == 200
    session.expire_all()
    rows = notifications_for(session, world["tech"], notification_type=NotificationType.SAMPLE_STATUS_CHANGED)
    assert sorted(str(n.resource_id) for n, _ in rows) == sorted(
        [str(world["samples"]["received"].id), str(world["samples"]["processing"].id)])
    # The submitter is the actor: no sample notification for them.
    assert notifications_for(session, world["author"], notification_type=NotificationType.SAMPLE_STATUS_CHANGED) == []
    # The reviewers' submission notification is unchanged.
    assert notifications_for(session, world["reviewer"], notification_type=NotificationType.REPORT_SUBMITTED)


def test_already_ready_samples_produce_no_event_and_no_audit_field(client, session, world):
    session.execute(text("UPDATE sample SET state = 'READY' WHERE order_id = :o AND removed_at IS NULL"),
                    {"o": str(world["order"].id)})
    session.commit()
    assert submit(client, world).status_code == 200
    assert auto_events(session) == []
    [audit] = submit_audits(session, world["report"].id)
    assert "samples_marked_ready" not in audit


def test_order_timeline_sample_timeline_and_ui_reads_show_ready(client, session, world):
    assert submit(client, world).status_code == 200
    headers = auth_headers(world["tech"])
    s = world["samples"]

    full = client.get(f"{API}/reports/{world['report'].id}/full", headers=auth_headers(world["author"])).json()
    assert {x["sample_code"]: x["state"] for x in full["samples"]} == {
        "S-REC": "READY", "S-PRO": "READY", "S-RDY": "READY", "S-DAM": "DAMAGED", "S-CAN": "CANCELLED"}

    order_full = client.get(f"{LAB}/orders/{world['order'].id}/full", headers=headers)
    assert order_full.status_code == 200, order_full.text
    assert order_full.json()["order"]["status"] == "REVIEW"
    assert {x["sample_code"]: x["state"] for x in order_full.json()["samples"]}["S-REC"] == "READY"

    detail = client.get(f"{LAB}/samples/{s['received'].id}", headers=headers)
    assert detail.status_code == 200 and detail.json()["state"] == "READY"

    events = client.get(f"{LAB}/samples/{s['received'].id}/events", headers=headers).json()["events"]
    auto = [e for e in events if e["event_type"] == "SAMPLE_STATE_CHANGED"
            and (e.get("metadata") or e.get("event_metadata") or {}).get("trigger") == "report_submitted"]
    assert len(auto) == 1
    order_events = client.get(f"{LAB}/orders/{world['order'].id}/events", headers=headers).json()["events"]
    types = [e["event_type"] for e in order_events]
    # The submission precedes the sample changes it caused.
    assert types.index("REPORT_SUBMITTED") < max(i for i, t in enumerate(types) if t == "SAMPLE_STATE_CHANGED")


# --------------------------------------------------------------------------- failure, retry and concurrency


def test_blocked_submission_changes_no_sample_and_writes_no_event(client, session, world):
    session.execute(text("DELETE FROM report_review WHERE order_id = :o"), {"o": str(world["order"].id)})
    session.commit()
    res = submit(client, world)
    assert res.status_code == 400, res.text
    session.rollback()
    assert all_states(session, world) == INITIAL
    assert auto_events(session) == []
    assert submissions(session, world["order"].id) == []


def test_a_failure_after_the_transition_rolls_everything_back(fresh_client, engine, session, world, monkeypatch):
    import app.api.v1.reports as reports_module

    def boom(*args, **kwargs):
        if kwargs.get("action") == "REPORT.SUBMIT":
            raise RuntimeError("synthetic failure after the sample transition")

    monkeypatch.setattr(reports_module, "_create_audit_log", boom)
    with pytest.raises(RuntimeError):
        submit(fresh_client, world)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT status::text FROM report WHERE id = :r"),
                            {"r": str(world["report"].id)}).scalar() == "DRAFT"
        assert all_states(conn, world) == INITIAL
        assert auto_events(conn) == []
        assert submissions(conn, world["order"].id) == []
        assert conn.execute(text("SELECT count(*) FROM notification WHERE type = 'SAMPLE_STATUS_CHANGED'")).scalar() == 0

    monkeypatch.undo()
    assert submit(fresh_client, world).status_code == 200  # the retry succeeds once
    with engine.connect() as conn:
        assert all_states(conn, world) == AFTER
        assert len(auto_events(conn)) == 2


def test_a_retried_submission_is_refused_and_duplicates_nothing(client, session, world):
    assert submit(client, world).status_code == 200
    again = submit(client, world)
    assert again.status_code == 400, again.text
    assert len(auto_events(session)) == 2
    assert len(submissions(session, world["order"].id)) == 1
    assert len(submit_audits(session, world["report"].id)) == 1


def test_concurrent_submissions_are_serialised_and_write_one_set_of_events(fresh_client, engine, session, world):
    order_id = str(world["order"].id)
    holder = engine.connect()
    tx = holder.begin()
    holder.execute(text('SELECT 1 FROM "order" WHERE id = :i FOR NO KEY UPDATE'), {"i": order_id})
    results = []

    def run():
        results.append(submit(fresh_client, world).status_code)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(0.8)  # both requests are queued on the order lock
    tx.commit()
    holder.close()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive()
    assert sorted(results) == [200, 400]
    with engine.connect() as conn:
        assert all_states(conn, world) == AFTER
        assert len(auto_events(conn)) == 2
        assert len(submissions(conn, order_id)) == 1


def test_a_concurrent_manual_insuficiente_is_never_overwritten(fresh_client, engine, session, world):
    """The manual state route does not take the order lock. A manual DAMAGED written (uncommitted) before the
    submission reaches the samples makes the submission wait on the row; after the commit PostgreSQL re-evaluates
    the state filter and the submission leaves the sample DAMAGED."""
    target = world["samples"]["processing"]
    holder = engine.connect()
    tx = holder.begin()
    holder.execute(text("UPDATE sample SET state = 'DAMAGED' WHERE id = :i"), {"i": str(target.id)})
    out = {}

    def run():
        out["status"] = submit(fresh_client, world).status_code

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.8)  # the submission holds the order lock and waits on the sample row
    assert thread.is_alive()
    tx.commit()
    holder.close()
    thread.join(timeout=60)
    assert out["status"] == 200
    with engine.connect() as conn:
        assert states(conn, target) == ["DAMAGED"]
        assert states(conn, world["samples"]["received"]) == ["READY"]
        assert [e[1]["sample_code"] for e in auto_events(conn)] == ["S-REC"]


# --------------------------------------------------------------------------- return to draft, resubmission


@pytest.mark.parametrize("exit_route", ["withdraw", "request-changes"])
def test_return_to_draft_keeps_ready_and_resubmission_reapplies_the_rule(client, session, world, exit_route):
    s = world["samples"]
    assert submit(client, world).status_code == 200
    if exit_route == "withdraw":
        res = withdraw(client, world)
    else:
        res = client.post(f"{API}/reports/{world['report'].id}/request-changes", json={"comment": "Corregir"},
                          headers=auth_headers(world["reviewer"]))
    assert res.status_code == 200, res.text
    assert order_status(session, world["order"].id) == OrderStatus.DIAGNOSIS.value
    # Proposed: a return to draft does not undo readiness.
    assert all_states(session, world) == AFTER
    assert len(auto_events(session)) == 2

    # While in draft a technician sets a sample back to En proceso by hand.
    manual = client.patch(f"{LAB}/samples/{s['received'].id}/state", json={"state": "PROCESSING"},
                          headers=auth_headers(world["tech"]))
    assert manual.status_code == 200, manual.text
    assert save(client, world).status_code == 200
    assert submit(client, world).status_code == 200
    assert all_states(session, world) == AFTER
    events = auto_events(session)
    assert len(events) == 3
    first, second = submissions(session, world["order"].id)
    assert events[-1][1]["submission_id"] == second
    assert events[-1][1]["old_state"] == "PROCESSING"
    assert events[-1][0] == str(s["received"].id)


def test_a_cancelled_order_is_not_recalculated_and_its_samples_are_untouched(client, session, world):
    session.execute(text("UPDATE \"order\" SET status = 'CANCELLED' WHERE id = :o"), {"o": str(world["order"].id)})
    session.commit()
    res = submit(client, world)
    if res.status_code == 200:  # current behavior: submission itself is not refused on a cancelled order
        assert order_status(session, world["order"].id) == OrderStatus.CANCELLED.value
    assert all_states(session, world) == INITIAL
    assert auto_events(session) == []


# --------------------------------------------------------------------------- amendment


def test_amendment_submission_applies_the_same_rule_with_amendment_correlation(client, session, amendment_lab):
    from .test_report_amendments import _open, _save, _submit

    lab = amendment_lab
    tenant, branch, order = lab["tenant"], lab["branch"], lab["order"]
    # After publication a sample was set back by hand (the manual route has no lifecycle guard) and a new sample
    # was added (creation is not refused on a published order); both are RECEIVED/PROCESSING at amendment time.
    session.execute(text("UPDATE sample SET state = 'PROCESSING' WHERE order_id = :o"), {"o": str(order.id)})
    late = create_sample(session, tenant, branch, order, sample_code="S-LATE")
    damaged = create_sample(session, tenant, branch, order, sample_code="S-DAM", state=SampleState.DAMAGED)
    session.commit()

    assert _open(client, lab).status_code == 200
    assert _save(client, lab).status_code == 200
    res = _submit(client, lab)
    assert res.status_code == 200, res.text
    session.expire_all()
    assert order_status(session, order.id) == OrderStatus.REVIEW.value
    assert states(session, late, damaged) == ["READY", "DAMAGED"]
    events = auto_events(session, order.id)
    assert len(events) == 2
    for _, meta, _ in events:
        assert meta["amendment_no"] == 1
        assert meta.get("amendment_id")
    [audit] = [a for a in submit_audits(session, lab["report"].id) if a.get("amendment_no") == 1]
    assert len(audit["samples_marked_ready"]) == 2


def test_a_caller_without_reports_submit_changes_no_sample(client, session, world):
    """The transition is a consequence of the submission and is gated only by it (reports:submit and tenant), like
    the first-image automation is gated by the upload. lab_tech lacks reports:submit: 403 and nothing changes."""
    denied = submit(client, world, actor=world["tech"])
    assert denied.status_code == 403
    assert all_states(session, world) == INITIAL
    assert auto_events(session) == []
    assert submit(client, world).status_code == 200
    assert all_states(session, world) == AFTER


def test_a_foreign_tenant_submitter_is_refused_and_changes_nothing(client, session, world):
    outsider_tenant = create_tenant(session, name="Outsider")
    outsider = create_user(session, outsider_tenant, email="path@outsider.example", roles=("pathologist",))
    res = submit(client, world, actor=outsider)
    assert res.status_code == 403, res.text
    assert all_states(session, world) == INITIAL
    assert auto_events(session) == []
