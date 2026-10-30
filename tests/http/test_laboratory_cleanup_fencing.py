"""Execution fencing of the laboratory cleanup (Céluma 1.4, #39; review of revision 1, finding R1).

A worker that was cancelled, superseded or whose lease expired must be unable to run a destructive phase or to
overwrite the current owner's phase/status/outbox, and must never report a failure, completion or cancellation
that is not true. These tests drive the executor's phases directly and deterministically (explicit owners,
explicit lease expiry, real threads where the interleaving matters) against real PostgreSQL.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.services import laboratory_cleanup as service
from app.services import laboratory_cleanup_scope as scope_mod
from app.services.laboratory_cleanup import CleanupError, LeaseLost

from .cleanup_world import Flow, snapshot
from .conftest import FakeS3Service
from .numbering_support import fresh_client, new_client  # noqa: F401
from .test_laboratory_cleanup_recovery import expire_lease, flow, op_row, queue_operation, scoped_total, world  # noqa: F401


def orders(session, world) -> int:
    session.rollback()
    return session.execute(text('SELECT count(*) FROM "order" WHERE tenant_id = :t'), {"t": str(world.target.tenant.id)}).scalar()


def audit_count(session, action) -> int:
    session.rollback()
    return session.execute(text("SELECT count(*) FROM audit_log WHERE action = :a"), {"a": action}).scalar()


class TestSupersededWorker:
    def test_a_replaced_worker_can_neither_delete_nor_overwrite_the_new_owner(self, session, world):
        bind = session.get_bind()
        op = queue_operation(session, world)
        a = service._claim(bind, op.id, "owner-a")
        service._phase_quiesce(bind, a)
        expire_lease(session, op.id)
        b = service._claim(bind, op.id, "owner-b")  # takeover of the expired lease
        assert b is not None and op_row(session, op.id).lease_owner == "owner-b"
        before = orders(session, world)
        assert before > 0

        assert service._phase_database(bind, a, "owner-a") is None   # refused: it is not the owner
        assert orders(session, world) == before

        # None of the old owner's writes lands: no phase move, no failure, no completion, no stale audit.
        tenant, actor = op.tenant_id, op.actor_user_id
        with pytest.raises(LeaseLost):
            service._set_phase(bind, op.id, "owner-a", "OBJECTS")
        service._fail(bind, op.id, tenant, actor, CleanupError("stale_failure", "no debe aparecer"), "owner-a")
        with pytest.raises(LeaseLost):
            service._complete(bind, a, "owner-a")
        row = op_row(session, op.id)
        assert (row.status, row.phase, row.lease_owner, row.error_code) == ("RUNNING", "DATABASE", "owner-b", None)
        assert audit_count(session, "LABORATORY_CLEANUP.FAILED") == 0
        assert audit_count(session, "LABORATORY_CLEANUP.COMPLETED") == 0

        # The legitimate owner is unaffected and finishes the whole thing exactly once.
        assert service.run_operation(bind, op.id, owner="owner-b") == "COMPLETED"
        assert orders(session, world) == 0
        assert audit_count(session, "LABORATORY_CLEANUP.DATABASE_COMPLETED") == 1
        assert audit_count(session, "LABORATORY_CLEANUP.COMPLETED") == 1

    def test_run_operation_of_a_superseded_worker_stops_silently(self, session, world, monkeypatch):
        """The worker that lost ownership returns None: no FAILED, no COMPLETED, no state written."""
        bind = session.get_bind()
        op = queue_operation(session, world)
        real = service._phase_database

        def lose_ownership_first(bind_, op_, owner_):
            expire_lease(session, op.id)
            assert service._claim(bind_, op.id, "replacement") is not None
            return real(bind_, op_, owner_)

        monkeypatch.setattr(service, "_phase_database", lose_ownership_first)
        assert service.run_operation(bind, op.id, owner="old-owner") is None
        row = op_row(session, op.id)
        assert (row.status, row.lease_owner, row.error_code) == ("RUNNING", "replacement", None)
        assert orders(session, world) > 0
        monkeypatch.setattr(service, "_phase_database", real)
        assert service.run_operation(bind, op.id, owner="replacement") == "COMPLETED"

    def test_a_stale_cancellation_by_the_old_owner_does_nothing(self, session, world):
        bind = session.get_bind()
        op = queue_operation(session, world)
        a = service._claim(bind, op.id, "owner-a")
        expire_lease(session, op.id)
        assert service._claim(bind, op.id, "owner-b") is not None
        assert service._cancel_row(bind, op.id, a.tenant_id, a.actor_user_id, code="preview_stale", message="x", owner="owner-a") is False
        assert op_row(session, op.id).status == "RUNNING"
        # and an OPERATOR cancellation is refused while a worker holds a live lease
        assert service._cancel_row(bind, op.id, a.tenant_id, a.actor_user_id, code="cancelled_by_operator", message="x") is False
        assert op_row(session, op.id).status == "RUNNING"


class TestCancellationVersusTheDatabasePhase:
    def test_cancellation_and_takeover_wait_for_a_running_database_phase_and_are_then_refused(self, session, world, flow, monkeypatch):
        """The lease EXPIRES while the destructive transaction is running (a long phase). An operator's cancellation
        and a replacement worker's takeover both arrive meanwhile; the transaction holds the operation row, so they
        wait, and afterwards the cancellation is refused (the database is already clean) and the old owner's later
        object phase is fenced off by the replacement."""
        monkeypatch.setattr(settings, "laboratory_cleanup_lease_seconds", 1)
        bind = session.get_bind()
        op = queue_operation(session, world)
        a = service._claim(bind, op.id, "owner-a")
        service._phase_quiesce(bind, a)
        real = scope_mod.delete_graph
        inside = threading.Event()
        outcome = {}

        def slow_delete(session_, scope, **kwargs):
            done = real(session_, scope, **kwargs)
            inside.set()
            time.sleep(2.2)  # longer than the 1 s lease: it expires while the transaction is still open
            return done

        monkeypatch.setattr(scope_mod, "delete_graph", slow_delete)

        def cancel():
            client = new_client()
            outcome["cancel"] = client.post(f"{flow.base}/operations/{op.id}/cancel", headers=flow.headers)

        def takeover():
            outcome["takeover"] = service._claim(bind, op.id, "owner-b")

        runner = threading.Thread(target=lambda: outcome.setdefault("db", service._phase_database(bind, a, "owner-a")))
        runner.start()
        assert inside.wait(timeout=30)
        time.sleep(1.3)  # the lease is now expired, the transaction is still running
        threads = [threading.Thread(target=cancel), threading.Thread(target=takeover)]
        [t.start() for t in threads]
        time.sleep(0.6)
        assert all(t.is_alive() for t in threads), "both must be waiting on the operation row, not racing the transaction"
        [t.join(timeout=60) for t in threads + [runner]]
        assert outcome["db"] is True
        assert outcome["cancel"].status_code == 409 and outcome["cancel"].json()["detail"]["code"] == "cancel_not_allowed"
        row = op_row(session, op.id)
        assert row.status == "RUNNING" and row.database_cleaned_at is not None and row.phase == "OBJECTS"
        assert orders(session, world) == 0
        # The commit renewed the owner's lease in the same statement, so the waiting takeover found a LIVE lease
        # and was refused: the owner who did the destructive work keeps the operation through its object phase.
        assert outcome["takeover"] is None and row.lease_owner == "owner-a"
        monkeypatch.setattr(scope_mod, "delete_graph", real)
        # Only if it later really dies (lease expires) can another worker take over, and then the old owner is fenced off.
        expire_lease(session, op.id)
        assert service._claim(bind, op.id, "owner-b") is not None
        with pytest.raises(LeaseLost):
            service._phase_objects(bind, a, "owner-a")
        assert service.run_operation(bind, op.id, owner="owner-b") == "COMPLETED"
        assert audit_count(session, "LABORATORY_CLEANUP.CANCELLED") == 0   # no false cancellation report
        assert audit_count(session, "LABORATORY_CLEANUP.COMPLETED") == 1

    def test_a_slow_but_alive_worker_keeps_its_lease_through_the_heartbeat(self, session, world, monkeypatch):
        monkeypatch.setattr(settings, "laboratory_cleanup_lease_seconds", 1)
        bind = session.get_bind()
        op = queue_operation(session, world)
        a = service._claim(bind, op.id, "owner-a")
        with service._Heartbeat(bind, op.id, "owner-a") as beat:
            time.sleep(2.6)  # > 2 lease periods with nothing else renewing it
            beat.check()
        row = op_row(session, op.id)
        assert row.lease_owner == "owner-a" and row.lease_expires_at > datetime.utcnow() - timedelta(seconds=1)
        assert a is not None

    def test_the_heartbeat_notices_when_ownership_is_gone(self, session, world, monkeypatch):
        monkeypatch.setattr(settings, "laboratory_cleanup_lease_seconds", 1)
        bind = session.get_bind()
        op = queue_operation(session, world)
        service._claim(bind, op.id, "owner-a")
        with service._Heartbeat(bind, op.id, "owner-a") as beat:
            expire_lease(session, op.id)
            assert service._claim(bind, op.id, "owner-b") is not None
            assert beat.lost.wait(timeout=5)
            with pytest.raises(LeaseLost):
                beat.check()
        assert op_row(session, op.id).lease_owner == "owner-b"  # the old heartbeat never renewed the new owner's lease... or its own

    def test_a_long_database_phase_completes_without_failing_or_being_taken_over(self, session, world, monkeypatch):
        monkeypatch.setattr(settings, "laboratory_cleanup_lease_seconds", 1)
        bind = session.get_bind()
        op = queue_operation(session, world)
        real = scope_mod.delete_graph

        def slow(session_, scope, **kwargs):
            done = real(session_, scope, **kwargs)
            time.sleep(2.3)
            return done

        monkeypatch.setattr(scope_mod, "delete_graph", slow)
        assert service.run_operation(bind, op.id, owner="steady") == "COMPLETED"
        row = op_row(session, op.id)
        assert (row.status, row.attempts, row.error_code) == ("COMPLETED", 1, None)
        assert orders(session, world) == 0


class TestObjectPhaseOwnership:
    def test_expiry_during_object_work_hands_over_cleanly_and_the_old_owner_writes_nothing(self, session, world, monkeypatch):
        bind = session.get_bind()
        op = queue_operation(session, world)
        a = service._claim(bind, op.id, "owner-a")
        service._phase_quiesce(bind, a)
        assert service._phase_database(bind, a, "owner-a") is True
        pending = session.execute(text("SELECT count(*) FROM laboratory_cleanup_object WHERE status = 'PENDING'")).scalar()
        assert pending > 0
        real = FakeS3Service.delete_object
        calls = {"n": 0}

        def expire_then_delete(self_, key):
            calls["n"] += 1
            if calls["n"] == 1:  # the lease expires and another worker takes over WHILE this call is in flight
                expire_lease(session, op.id)
                assert service._claim(bind, op.id, "owner-b") is not None
            return real(self_, key)

        monkeypatch.setattr(FakeS3Service, "delete_object", expire_then_delete)
        with pytest.raises(LeaseLost):
            service._phase_objects(bind, a, "owner-a")
        session.rollback()
        # The old owner recorded nothing for the object it deleted; the new owner sees it already gone.
        statuses = dict(session.execute(text("SELECT status, count(*) FROM laboratory_cleanup_object GROUP BY status")).all())
        assert statuses.get("PENDING") == pending
        monkeypatch.setattr(FakeS3Service, "delete_object", real)
        assert service.run_operation(bind, op.id, owner="owner-b") == "COMPLETED"
        final = dict(session.execute(text("SELECT status, count(*) FROM laboratory_cleanup_object GROUP BY status")).all())
        assert final == {"DELETED": pending - 1, "ABSENT": 1}
        assert audit_count(session, "LABORATORY_CLEANUP.COMPLETED") == 1
        assert audit_count(session, "LABORATORY_CLEANUP.FAILED") == 0
        assert set(world.target.scoped_keys).isdisjoint(snapshot_keys())

    def test_a_cancelled_operation_never_reaches_the_object_phase(self, session, world, flow):
        bind = session.get_bind()
        op = queue_operation(session, world)
        a = service._claim(bind, op.id, "owner-a")
        expire_lease(session, op.id)
        assert flow.cancel(str(op.id)).json()["status"] == "CANCELLED"
        assert service._phase_database(bind, a, "owner-a") is None
        with pytest.raises(LeaseLost):
            service._phase_objects(bind, a, "owner-a")
        assert scoped_total(session, world) > 0


def snapshot_keys():
    from .cleanup_world import object_store
    return set(object_store())
