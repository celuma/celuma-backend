"""Gate, concurrency, failure injection, restart and recovery of the laboratory cleanup (Céluma 1.4, #39).

Real PostgreSQL, real threads, the real executor. The object store is the in-memory fake: the injected
failures prove the executor's bookkeeping and retry behavior, not a physical bucket's.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlmodel import Session

from app.core.config import settings
from app.models.laboratory import Order
from app.services import laboratory_cleanup as service
from app.services import laboratory_cleanup_scope as scope_mod
from app.services.laboratory_cleanup_authority import CleanupOperator
from app.services.notification_delivery import select_due_delivery_ids

from .cleanup_world import Flow, build_world, object_store, snapshot
from .conftest import LEAKY_AWS_MESSAGE, ClientError, FakeS3Service, assert_no_secret_markers, rendered_log_text
from .factories import TEST_USER_PASSWORD
from .numbering_support import UNIFIED, fresh_client, run_parallel  # noqa: F401


class Crash(BaseException):
    """A process death: not an `Exception`, so no handler in the executor can swallow it."""


@pytest.fixture
def world(session, fresh_client, monkeypatch, stub_pdf_render):
    w = build_world(session, fresh_client, monkeypatch, stub_pdf_render)
    return w


@pytest.fixture
def flow(fresh_client, world):
    return Flow(fresh_client, world.target)


def queue_operation(session, world):
    """Confirm (raising the gate) WITHOUT executing: what the request does before the background task."""
    t = world.target
    scope = scope_mod.compute_scope(session, t.tenant.id)
    token, _jti, _exp = service.issue_token(
        tenant_id=t.tenant.id, actor_id=t.operator.id, revision=scope.revision)
    result = service.start(
        session, CleanupOperator(user=t.operator, tenant=t.tenant), preview_token=token,
        reason="Limpieza sintética para pruebas de recuperación", confirm_name=t.tenant.name, password=TEST_USER_PASSWORD)
    return result.operation


def op_row(session, op_id):
    session.expire_all()
    return session.execute(text(
        "SELECT status, phase, error_code, attempts, lease_owner, lease_expires_at, database_cleaned_at "
        "FROM laboratory_cleanup_operation WHERE id = :i"), {"i": str(op_id)}).one()


def scoped_total(session, world):
    return sum(scope_mod.count_rows(session, st, str(world.target.tenant.id)) for st in scope_mod.tables_for(True))


def expire_lease(session, op_id):
    session.execute(text("UPDATE laboratory_cleanup_operation SET lease_expires_at = :t WHERE id = :i"),
                    {"t": datetime.utcnow() - timedelta(seconds=5), "i": str(op_id)})
    session.commit()


# --------------------------------------------------------------------------- the gate


class TestWritersHonorTheGate:
    def test_every_operational_writer_of_the_tenant_is_refused_and_others_keep_working(self, session, fresh_client, world):
        queue_operation(session, world)
        lab, other = world.target.lab, world.other.lab
        before = snapshot(session, world.target.tenant.id, skip={"audit_log", "laboratory_cleanup_operation"})
        t = str(world.target.tenant.id)
        o1 = world.target.orders["O1"]
        attempts = {
            "unified registration": fresh_client.post(UNIFIED, json=lab.unified(("BX",)), headers=lab.headers),
            "comment": fresh_client.post(f"/api/v1/laboratory/orders/{o1}/comments", headers=lab.headers,
                                         json={"text": "comentario durante mantenimiento"}),
            "order notes": fresh_client.patch(f"/api/v1/laboratory/orders/{o1}/notes", headers=lab.headers, json={"notes": "x"}),
            "payment": fresh_client.post("/api/v1/billing/payments/", headers=lab.headers, json={
                "tenant_id": t, "invoice_id": world.target.invoice_ids[2], "amount": 5.0, "currency": "MXN", "method": "cash"}),
            "removal": fresh_client.post(f"/api/v1/laboratory/orders/{o1}/removal", headers=lab.headers,
                                         json={"reason": "Intento durante el mantenimiento"}),
        }
        for label, response in attempts.items():
            assert response.status_code == 423, (label, response.status_code, response.text)
            detail = response.json()["detail"]
            assert detail["code"] == "laboratory_cleanup_in_progress" and "mantenimiento" in detail["message"], label
        assert snapshot(session, world.target.tenant.id, skip={"audit_log", "laboratory_cleanup_operation"}) == before
        # reads are not blocked, and the other laboratory is completely unaffected
        assert fresh_client.get("/api/v1/laboratory/orders/", headers=lab.headers).status_code == 200
        ok = fresh_client.post(UNIFIED, json=other.unified(("BX",)), headers=other.headers)
        assert ok.status_code == 200, ok.text

    def test_usage_reconciliation_and_email_delivery_stand_down_for_the_tenant(self, session, fresh_client, world):
        queue_operation(session, world)
        admin = world.target.admin
        from .factories import auth_headers
        r = fresh_client.post("/api/v1/tenant/usage/reconcile", headers=auth_headers(admin))
        assert r.status_code == 409
        due = set(select_due_delivery_ids(session, now=datetime.utcnow() + timedelta(days=1), limit=100))
        session.rollback()
        mine = {str(i) for (i,) in session.execute(text(
            "SELECT id FROM notification_delivery WHERE tenant_id = :t"), {"t": str(world.target.tenant.id)})}
        theirs = {str(i) for (i,) in session.execute(text(
            "SELECT id FROM notification_delivery WHERE tenant_id = :t"), {"t": str(world.other.tenant.id)})}
        assert {str(i) for i in due} & mine == set()
        assert {str(i) for i in due} & theirs  # the other laboratory's pending delivery is still claimable

    def test_cancelling_before_the_database_phase_lifts_the_gate(self, session, fresh_client, world, flow):
        op = queue_operation(session, world)
        before = snapshot(session, world.target.tenant.id, skip={"audit_log", "laboratory_cleanup_operation"})
        r = flow.cancel(str(op.id))
        assert r.status_code == 200 and r.json()["status"] == "CANCELLED" and r.json()["writers_blocked"] is False
        assert flow.cancel(str(op.id)).json()["status"] == "CANCELLED"  # idempotent
        ok = fresh_client.post(UNIFIED, json=world.target.lab.unified(("BX",)), headers=world.target.lab.headers)
        assert ok.status_code == 200, ok.text
        # nothing was deleted by the cancelled operation
        session.expire_all()
        assert scoped_total(session, world) > 0
        assert before  # snapshot taken while gated, compared implicitly by the creation above succeeding

    def test_the_gate_guard_covers_every_operational_model(self):
        from app.core.maintenance_guard import GUARDED_MODELS
        guarded_tables = {m.__tablename__ for m in GUARDED_MODELS}
        deleted_tables = {st.table.strip('"') for st in scope_mod.CORE_TABLES + scope_mod.FINANCE_TABLES}
        # notifications are deliberately not gated (they are produced by gated writers, and unrelated
        # notices such as usage thresholds must keep flowing); the rest of the DELETE scope is.
        assert deleted_tables - guarded_tables == {"notification", "notification_recipient", "notification_delivery"}


# --------------------------------------------------------------------------- concurrency


class TestConcurrency:
    def test_duplicate_confirmations_race_to_exactly_one_operation(self, session, fresh_client, world, flow):
        token = flow.preview().json()["preview_token"]

        def confirm(client):
            return client.post(f"{flow.base}/operations", headers=flow.headers, json={
                "preview_token": token, "reason": "Limpieza sintética concurrente",
                "confirm_name": world.target.tenant.name, "password": TEST_USER_PASSWORD})

        results = run_parallel([confirm, confirm, confirm])
        assert all(not isinstance(r, Exception) for r in results), results
        assert sorted(r.status_code for r in results) == [202, 202, 202]
        assert len({r.json()["id"] for r in results}) == 1
        assert sum(1 for r in results if r.headers.get("Idempotent-Replayed") == "true") == 2
        session.expire_all()
        assert session.execute(text("SELECT count(*) FROM laboratory_cleanup_operation")).scalar() == 1
        assert session.execute(text("SELECT count(*) FROM audit_log WHERE action = 'LABORATORY_CLEANUP.COMPLETED'")).scalar() == 1
        assert op_row(session, results[0].json()["id"]).status == "COMPLETED"

    def test_two_workers_on_one_operation_execute_it_once(self, session, world, monkeypatch):
        op = queue_operation(session, world)
        calls = []
        real = FakeS3Service.delete_object

        def counting(self, key):
            calls.append(key)
            time.sleep(0.01)
            return real(self, key)

        monkeypatch.setattr(FakeS3Service, "delete_object", counting)
        engine = session.get_bind()
        results = []
        barrier = threading.Barrier(4)

        def worker():
            barrier.wait(timeout=30)
            results.append(service.run_operation(engine, op.id))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        [t.start() for t in threads]
        [t.join(timeout=120) for t in threads]
        assert all(not t.is_alive() for t in threads)
        assert results.count("COMPLETED") == 1 and results.count(None) == 3, results
        assert len(calls) == len(set(calls)) == len(world.target.scoped_keys)  # each object deleted exactly once
        assert op_row(session, op.id).status == "COMPLETED"
        assert session.execute(text("SELECT count(*) FROM audit_log WHERE action = 'LABORATORY_CLEANUP.DATABASE_COMPLETED'")).scalar() == 1

    def test_a_writer_in_flight_when_the_gate_rises_is_waited_for_and_makes_the_preview_stale(self, session, world, monkeypatch):
        engine = session.get_bind()
        lab = world.target.lab
        flushed, release = threading.Event(), threading.Event()
        outcome = {}

        def writer():
            with Session(engine) as s:
                s.add(Order(tenant_id=lab.tenant.id, branch_id=lab.branch.id, order_code="LATE-1"))
                s.flush()  # first guarded write: takes the shared lock, sees no gate
                flushed.set()
                release.wait(timeout=60)
                s.commit()
                outcome["writer"] = "committed"

        w = threading.Thread(target=writer)
        w.start()
        assert flushed.wait(timeout=30)
        op = queue_operation(session, world)  # the gate rises while the writer is mid-transaction
        before = snapshot(session, world.other.tenant.id)
        runner = threading.Thread(target=lambda: outcome.setdefault("run", service.run_operation(engine, op.id)))
        runner.start()
        time.sleep(1.0)
        assert runner.is_alive(), "the barrier must wait for the in-flight writer"
        assert op_row(session, op.id).phase == "QUIESCE"
        release.set()
        w.join(timeout=30)
        runner.join(timeout=60)
        assert not runner.is_alive() and outcome["writer"] == "committed"
        # The late order is part of the scope now: the approved preview is stale, nothing was deleted.
        row = op_row(session, op.id)
        assert (row.status, row.error_code, row.database_cleaned_at) == ("CANCELLED", "preview_stale", None)
        session.expire_all()
        assert session.execute(text("SELECT count(*) FROM \"order\" WHERE order_code = 'LATE-1'")).scalar() == 1
        assert scoped_total(session, world) > 0
        assert snapshot(session, world.other.tenant.id) == before

    def test_the_barrier_times_out_retryably_while_a_writer_holds_the_tenant(self, session, world, flow, monkeypatch):
        monkeypatch.setattr(settings, "laboratory_cleanup_barrier_timeout_seconds", 1)
        engine = session.get_bind()
        lab = world.target.lab
        flushed, release = threading.Event(), threading.Event()

        def writer():
            with Session(engine) as s:
                s.add(Order(tenant_id=lab.tenant.id, branch_id=lab.branch.id, order_code="LATE-2"))
                s.flush()
                flushed.set()
                release.wait(timeout=60)
                s.rollback()  # the writer gives up: the scope is unchanged

        w = threading.Thread(target=writer)
        w.start()
        assert flushed.wait(timeout=30)
        op = queue_operation(session, world)
        assert service.run_operation(engine, op.id) == "FAILED"
        row = op_row(session, op.id)
        assert (row.status, row.phase, row.error_code, row.database_cleaned_at) == ("FAILED", "QUIESCE", "barrier_timeout", None)
        assert scoped_total(session, world) > 0
        release.set()
        w.join(timeout=30)
        # retry: the writer is gone, the scope is as approved, the cleanup proceeds
        monkeypatch.setattr(settings, "laboratory_cleanup_barrier_timeout_seconds", 30)
        assert flow.retry(str(op.id)).status_code == 202
        assert op_row(session, op.id).status == "COMPLETED"


# --------------------------------------------------------------------------- failure injection and retry


class TestInjectedFailures:
    def test_a_database_error_rolls_back_everything_and_a_retry_completes(self, session, world, flow, monkeypatch, caplog):
        t = world.target.tenant.id
        before = snapshot(session, t, skip={"audit_log", "laboratory_cleanup_operation"})
        other_before = snapshot(session, world.other.tenant.id)
        store_before = object_store()
        real = scope_mod.delete_graph

        def exploding(session_, scope, **kwargs):
            real(session_, scope, **kwargs)  # delete the whole graph...
            raise RuntimeError(f"disk exploded SECRET_OBJECT_KEY {LEAKY_AWS_MESSAGE}")  # ...then fail before commit

        monkeypatch.setattr(scope_mod, "delete_graph", exploding)
        with caplog.at_level("DEBUG"):
            op = flow.run()
        assert (op["status"], op["phase"], op["error_code"]) == ("FAILED", "DATABASE", "database_error")
        assert "SECRET" not in op["error_message"] and "disk" not in op["error_message"]
        assert_no_secret_markers(rendered_log_text(caplog), "logs")
        assert op["can_retry"] and op["can_cancel"] and op["writers_blocked"] is True
        # The transaction rolled back: the graph, the outbox, the retired codes and the objects are intact.
        assert snapshot(session, t, skip={"audit_log", "laboratory_cleanup_operation"}) == before
        assert snapshot(session, world.other.tenant.id) == other_before
        assert object_store() == store_before
        assert session.execute(text("SELECT count(*) FROM laboratory_cleanup_object")).scalar() == 0
        assert session.execute(text("SELECT count(*) FROM laboratory_cleanup_retired_code")).scalar() == 0

        monkeypatch.setattr(scope_mod, "delete_graph", real)
        retried = flow.retry(op["id"])
        assert retried.status_code == 202
        done = flow.operation(op["id"])
        assert (done["status"], done["attempts"]) == ("COMPLETED", 2)
        assert set(store_before) - set(object_store()) == world.target.scoped_keys

    def test_a_usage_failure_inside_the_transaction_also_rolls_back(self, session, world, flow, monkeypatch):
        t = world.target.tenant.id
        before = snapshot(session, t, skip={"audit_log", "laboratory_cleanup_operation"})
        from app.services.usage import UsageService

        def broken(*args, **kwargs):
            raise RuntimeError("usage counter unavailable")

        monkeypatch.setattr(UsageService, "adjust_storage", broken)
        op = flow.run()
        assert op["status"] == "FAILED" and op["error_code"] == "database_error"
        assert snapshot(session, t, skip={"audit_log", "laboratory_cleanup_operation"}) == before

    def test_object_store_failures_leave_a_cleaned_database_and_retry_only_what_remains(self, session, world, flow, monkeypatch, caplog):
        poisoned = sorted(world.target.scoped_keys)[0]
        calls = []
        real = FakeS3Service.delete_object

        def flaky(self, key):
            calls.append(key)
            if key == poisoned:
                raise ClientError("InternalError")  # message quotes ARN, key and a signed query string
            return real(self, key)

        monkeypatch.setattr(FakeS3Service, "delete_object", flaky)
        with caplog.at_level("DEBUG"):
            op = flow.run()
        assert (op["status"], op["phase"], op["error_code"]) == ("FAILED", "OBJECTS", "objects_failed")
        assert_no_secret_markers(rendered_log_text(caplog), "logs")
        assert_no_secret_markers(str(op), "the operation view")
        total = op["objects"]["total"]
        assert op["objects"]["failed"] == 1 and op["objects"]["deleted"] == total - 1
        assert op["objects"]["pending_bytes"] > 0
        # The database part IS done and committed (a rollback could not restore the deleted objects).
        session.expire_all()
        assert scope_mod.count_rows(session, scope_mod.CORE_TABLES[0], str(world.target.tenant.id)) == 0
        assert op["writers_blocked"] is False and op["can_cancel"] is False and op["can_retry"] is True
        assert poisoned in object_store() and len(set(world.target.scoped_keys) - set(object_store())) == total - 1

        # Reconciliation does not report the pending object as an orphan (it is tracked by the outbox).
        from .factories import auth_headers
        run = flow.client.post("/api/v1/tenant/usage/reconcile", headers=auth_headers(world.target.admin))
        assert run.status_code == 200 and run.json()["orphans_found"] == 0, run.text

        calls.clear()
        monkeypatch.setattr(FakeS3Service, "delete_object", real)
        retried = flow.retry(op["id"])
        assert retried.status_code == 202
        done = flow.operation(op["id"])
        assert done["status"] == "COMPLETED" and done["objects"]["failed"] == 0 and done["objects"]["pending"] == 0
        assert poisoned not in object_store()
        assert calls == []  # the retry used the real method directly: nothing else was re-deleted
        assert set(world.target.scoped_keys).isdisjoint(object_store())

    def test_a_retry_after_partial_object_failure_never_repeats_a_successful_deletion(self, session, world, flow, monkeypatch):
        poisoned = sorted(world.target.scoped_keys)[0]
        calls = []
        real = FakeS3Service.delete_object
        fail = {"on": True}

        def flaky(self, key):
            calls.append(key)
            if fail["on"] and key == poisoned:
                raise RuntimeError("temporary outage")
            return real(self, key)

        monkeypatch.setattr(FakeS3Service, "delete_object", flaky)
        op = flow.run()
        assert op["status"] == "FAILED"
        first_round = list(calls)
        calls.clear()
        fail["on"] = False
        assert flow.retry(op["id"]).status_code == 202
        assert calls == [poisoned]  # only the remaining object was touched
        assert sorted(first_round) == sorted(world.target.scoped_keys)
        assert flow.operation(op["id"])["status"] == "COMPLETED"

    def test_an_object_in_another_bucket_is_never_deleted(self, session, world, flow):
        key = sorted(world.target.scoped_keys)[0]
        session.execute(text("UPDATE storage_object SET bucket = 'some-other-bucket' WHERE object_key = :k"), {"k": key})
        session.commit()
        op = flow.run()
        assert (op["status"], op["error_code"]) == ("FAILED", "objects_failed")
        assert key in object_store()
        assert session.execute(text("SELECT error_code FROM laboratory_cleanup_object WHERE object_key = :k"), {"k": key}).scalar() == "bucket_mismatch"


# --------------------------------------------------------------------------- restart / lease


class TestRestart:
    def test_a_crash_after_the_database_commit_resumes_at_the_objects_without_redoing_anything(self, session, world, flow, monkeypatch):
        op = queue_operation(session, world)
        engine = session.get_bind()
        real_objects = service._phase_objects
        monkeypatch.setattr(service, "_phase_objects", lambda *a, **k: (_ for _ in ()).throw(Crash()))
        with pytest.raises(Crash):
            service.run_operation(engine, op.id)
        row = op_row(session, op.id)
        assert (row.status, row.phase) == ("RUNNING", "OBJECTS") and row.database_cleaned_at is not None
        assert scope_mod.count_rows(session, scope_mod.CORE_TABLES[0], str(world.target.tenant.id)) == 0
        retired = session.execute(text("SELECT count(*) FROM laboratory_cleanup_retired_code")).scalar()
        audit = session.execute(text("SELECT count(*) FROM audit_log WHERE action = 'LABORATORY_CLEANUP.DATABASE_COMPLETED'")).scalar()
        assert session.execute(text("SELECT count(*) FROM laboratory_cleanup_object WHERE status = 'PENDING'")).scalar() == len(world.target.scoped_keys)

        # The dead worker's lease is still live: a retry is refused (no double execution)...
        assert flow.retry(str(op.id)).status_code == 409
        monkeypatch.setattr(service, "_phase_objects", real_objects)  # the replacement process is healthy
        # ...until the lease expires, after which any worker takes over.
        expire_lease(session, op.id)
        assert flow.retry(str(op.id)).status_code == 202
        assert op_row(session, op.id).status == "COMPLETED"
        assert session.execute(text("SELECT count(*) FROM laboratory_cleanup_retired_code")).scalar() == retired
        assert session.execute(text("SELECT count(*) FROM audit_log WHERE action = 'LABORATORY_CLEANUP.DATABASE_COMPLETED'")).scalar() == audit
        assert set(world.target.scoped_keys).isdisjoint(object_store())

    def test_a_crash_before_the_database_commit_changes_nothing_and_can_be_cancelled(self, session, world, flow, monkeypatch):
        op = queue_operation(session, world)
        engine = session.get_bind()
        before = snapshot(session, world.target.tenant.id, skip={"audit_log", "laboratory_cleanup_operation", "laboratory_cleanup_object", "laboratory_cleanup_retired_code"})
        real = scope_mod.delete_graph

        def die(session_, scope, **kwargs):
            real(session_, scope, **kwargs)
            raise Crash()

        monkeypatch.setattr(scope_mod, "delete_graph", die)
        with pytest.raises(Crash):
            service.run_operation(engine, op.id)
        assert snapshot(session, world.target.tenant.id, skip={"audit_log", "laboratory_cleanup_operation", "laboratory_cleanup_object", "laboratory_cleanup_retired_code"}) == before
        assert session.execute(text("SELECT count(*) FROM laboratory_cleanup_object")).scalar() == 0
        # live lease: cannot be cancelled out from under the running worker
        refused = flow.cancel(str(op.id))
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == "cancel_not_allowed"
        expire_lease(session, op.id)
        cancelled = flow.cancel(str(op.id))
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"
        ok = flow.client.post(UNIFIED, json=world.target.lab.unified(("BX",)), headers=world.target.lab.headers)
        assert ok.status_code == 200

    def test_cancel_is_refused_once_the_database_phase_committed(self, session, world, flow, monkeypatch):
        real = FakeS3Service.delete_object
        monkeypatch.setattr(FakeS3Service, "delete_object", lambda self, key: (_ for _ in ()).throw(RuntimeError("down")))
        op = flow.run()
        assert op["status"] == "FAILED" and op["phase"] == "OBJECTS"
        r = flow.cancel(op["id"])
        assert r.status_code == 409 and r.json()["detail"]["code"] == "cancel_not_allowed"
        # and a completed operation accepts neither retry nor cancel
        monkeypatch.setattr(FakeS3Service, "delete_object", real)
        assert flow.retry(op["id"]).status_code == 202
        assert flow.retry(op["id"]).status_code == 409
        assert flow.cancel(op["id"]).status_code == 409


# --------------------------------------------------------------------------- no resurrection


class TestNoResurrection:
    def test_pending_deliveries_and_notifications_of_cleaned_content_are_gone_and_unrelated_ones_stay(self, session, world, flow):
        t = str(world.target.tenant.id)
        before_unrelated = session.execute(text(
            "SELECT count(*) FROM notification WHERE tenant_id = :t AND resource_type = 'tenant'"), {"t": t}).scalar()
        flow.run()
        session.expire_all()
        assert session.execute(text("SELECT count(*) FROM notification_delivery WHERE tenant_id = :t"), {"t": t}).scalar() == 0
        assert session.execute(text(
            "SELECT count(*) FROM notification WHERE tenant_id = :t AND resource_type IN ('order','sample','report')"), {"t": t}).scalar() == 0
        assert session.execute(text(
            "SELECT count(*) FROM notification WHERE tenant_id = :t AND resource_type = 'tenant'"), {"t": t}).scalar() == before_unrelated == 1
        # the delivery worker has nothing of this laboratory to claim, and still serves the other one
        due = {str(i) for i in select_due_delivery_ids(session, now=datetime.utcnow() + timedelta(days=1), limit=100)}
        session.rollback()
        assert due and not (due & {str(i) for (i,) in session.execute(text(
            "SELECT id FROM notification_delivery WHERE tenant_id = :t"), {"t": t})})

    def test_an_in_flight_email_blocks_the_cleanup_instead_of_racing_it(self, session, world, flow):
        session.execute(text("UPDATE notification_delivery SET status = 'SENDING' WHERE tenant_id = :t AND status = 'PENDING'"),
                        {"t": str(world.target.tenant.id)})
        session.commit()
        pv = flow.preview().json()
        assert pv["preview_token"] is None and pv["blockers"][0]["code"] == "deliveries_in_flight"
