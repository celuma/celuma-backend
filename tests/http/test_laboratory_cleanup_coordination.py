"""Reference and worker coordination of the laboratory cleanup (Céluma 1.4, #39; review of revision 1, R2 and R3).

R2 — a preserved configuration of ANY laboratory that names an object keeps it, whether the reference existed when
the scope was resolved or appeared later; the cleanup's physical deletion and the configuration writers are
serialized by the global asset lock. R3 — a notification claim or a usage reconciliation either finishes before the
maintenance barrier (and the cleanup then sees it and refuses) or is refused after the gate rose; other laboratories
are never held back. Deterministic: explicit events and real threads against PostgreSQL, synthetic data only, the
in-memory `FakeS3Service` (so object-store behaviour here is a model of the contract, not of AWS).
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlmodel import Session

from app.core import maintenance_guard
from app.models.report import ReportTemplate
from app.services import laboratory_cleanup as service
from app.services import laboratory_cleanup_scope as scope_mod
from app.services import notification_delivery
from app.services.usage_reconciliation import ConcurrentReconciliationError, UsageReconciliationService

from .conftest import FakeS3Service
from .numbering_support import fresh_client  # noqa: F401
from .test_laboratory_cleanup_recovery import flow, op_row, queue_operation, world  # noqa: F401


def processed_key(world) -> str:
    return next(k for k in sorted(world.target.scoped_keys) if "/processed/" in k)


def scoped_total_orders(session, world) -> int:
    session.rollback()
    return session.execute(text('SELECT count(*) FROM "order" WHERE tenant_id = :t'), {"t": str(world.target.tenant.id)}).scalar()


def physical_keys(session, world) -> set:
    scope = scope_mod.compute_scope(session, world.target.tenant.id)
    session.rollback()
    return {o.object_key for o in scope.objects.physical}


def run_until_objects(session, world):
    """Queue, then drive claim -> quiesce -> database only: the outbox exists, nothing is deleted from the store."""
    bind = session.get_bind()
    op = queue_operation(session, world)
    claimed = service._claim(bind, op.id, "step-worker")
    service._phase_quiesce(bind, claimed, "step-worker")
    assert service._phase_database(bind, claimed, "step-worker") is True
    return op, claimed, bind


# ----------------------------------------------------------------------------------------------------------- R2


class TestPreservedConfigurationOfAnyLaboratoryKeepsTheObject:
    """The scope resolution reads every preserved reference source of EVERY laboratory."""

    SOURCES = {
        "template_json": ("UPDATE report_template SET template_json = CAST(:v AS json) WHERE tenant_id = :t", "tmpl"),
        "template_version_configuration": ("UPDATE report_template_version SET configuration = CAST(:v AS json) WHERE tenant_id = :t", "cfg"),
        "letterhead_version_configuration": ("UPDATE report_letterhead_version SET configuration = CAST(:v AS json) WHERE tenant_id = :t", "cfg"),
        "tenant_logo_url": ("UPDATE tenant SET logo_url = :v WHERE id = :t", "url"),
        "user_avatar_url": ("UPDATE app_user SET avatar_url = :v WHERE tenant_id = :t", "url"),
    }

    @pytest.mark.parametrize("source", sorted(SOURCES))
    def test_a_reference_in_the_other_laboratory_keeps_the_object_out_of_the_physical_deletion(self, session, world, source):
        key = processed_key(world)
        assert key in physical_keys(session, world)  # the control: unreferenced, it IS scheduled
        statement, shape = self.SOURCES[source]
        value = (json.dumps({"presentation": {"header": {"logo_key": key}}}) if shape in ("tmpl", "cfg")
                 else f"https://cdn.example.invalid/{key}")
        updated = session.execute(text(statement), {"v": value, "t": str(world.other.tenant.id)}).rowcount
        assert updated >= 1, f"the synthetic world has no {source} row to point at the key"
        session.commit()
        assert key not in physical_keys(session, world)
        assert key in FakeS3Service.store

    def test_the_same_reference_in_the_target_laboratory_also_keeps_it(self, session, world):
        key = processed_key(world)
        session.execute(text("UPDATE report_template SET template_json = CAST(:v AS json) WHERE tenant_id = :t"),
                        {"v": json.dumps({"logo_key": key}), "t": str(world.target.tenant.id)})
        session.commit()
        assert key not in physical_keys(session, world)

    def test_a_reference_inside_a_removed_report_snapshot_does_not_keep_the_object(self, session, world):
        """`report.template` of a REMOVED report disappears with it; only surviving snapshots count."""
        key = processed_key(world)
        assert key in physical_keys(session, world)
        session.execute(text("UPDATE report SET template = CAST(:v AS json) WHERE tenant_id = :t"),
                        {"v": json.dumps({"logo_key": key}), "t": str(world.target.tenant.id)})
        session.commit()
        assert key in physical_keys(session, world)
        session.execute(text("UPDATE report SET template = CAST(:v AS json) WHERE tenant_id = :t"),
                        {"v": json.dumps({"logo_key": key}), "t": str(world.other.tenant.id)})
        session.commit()
        assert key not in physical_keys(session, world)


class TestLateReferences:
    """A reference that appears after the outbox was written."""

    def test_an_orm_writer_creating_a_reference_to_a_scheduled_object_is_refused(self, session, world):
        key = processed_key(world)
        op, claimed, bind = run_until_objects(session, world)
        session.rollback()
        scheduled = session.execute(text("SELECT status FROM laboratory_cleanup_object WHERE operation_id = :o AND object_key = :k"),
                                    {"o": str(op.id), "k": key}).scalar()
        assert scheduled == "PENDING"
        late = ReportTemplate(tenant_id=world.other.tenant.id, name="Plantilla tardía", template_json={"logo_key": key})
        session.add(late)
        with pytest.raises(HTTPException) as exc:
            session.flush()
        assert exc.value.status_code == 409 and exc.value.detail["code"] == "asset_scheduled_for_deletion"
        session.rollback()
        # an unrelated template is accepted: the refusal is about the scheduled key only
        session.add(ReportTemplate(tenant_id=world.other.tenant.id, name="Plantilla sin relación", template_json={"logo_key": "elsewhere/x.png"}))
        session.commit()

    def test_a_raw_reference_created_after_the_outbox_keeps_the_object_and_closes_its_row_as_kept(self, session, world):
        key = processed_key(world)
        op, claimed, bind = run_until_objects(session, world)
        # a path the ORM guard cannot see (hand-written SQL, a script, another service)
        session.execute(text("UPDATE report_template SET template_json = CAST(:v AS json) WHERE tenant_id = :t"),
                        {"v": json.dumps({"logo_key": key}), "t": str(world.other.tenant.id)})
        session.commit()
        service._phase_objects(bind, claimed, "step-worker")
        session.expire_all()
        statuses = dict(session.execute(text("SELECT object_key, status FROM laboratory_cleanup_object WHERE operation_id = :o"),
                                        {"o": str(op.id)}).all())
        assert statuses[key] == "KEPT"
        assert key in FakeS3Service.store, "the late-referenced object must survive"
        others = {k: s for k, s in statuses.items() if k != key}
        assert others and set(others.values()) <= {"DELETED", "ABSENT"}
        assert not [k for k in others if k in FakeS3Service.store]

    def test_a_writer_that_commits_before_the_database_phase_resolves_its_objects_is_seen_and_the_object_stays(self, session, world):
        """Interleaving 1: the writer holds the SHARED asset lock; the database phase waits for it, then sees the
        reference. The reference changes what the approved preview covered, so the operation is cancelled as stale
        BEFORE deleting anything — and in no case is the referenced object scheduled."""
        key = processed_key(world)
        bind = session.get_bind()
        flushed, release, outcome = threading.Event(), threading.Event(), {}

        def writer():
            with Session(bind) as s:
                s.add(ReportTemplate(tenant_id=world.other.tenant.id, name="Plantilla concurrente", template_json={"logo_key": key}))
                s.flush()  # takes the shared asset lock; the key is not scheduled yet
                flushed.set()
                release.wait(timeout=60)
                s.commit()
                outcome["writer"] = "committed"

        w = threading.Thread(target=writer)
        w.start()
        assert flushed.wait(timeout=30)
        op = queue_operation(session, world)
        claimed = service._claim(bind, op.id, "step-worker")
        service._phase_quiesce(bind, claimed, "step-worker")
        runner = threading.Thread(target=lambda: outcome.setdefault("db", service._phase_database(bind, claimed, "step-worker")))
        runner.start()
        time.sleep(1.0)
        assert runner.is_alive(), "the database phase must wait for the in-flight configuration writer"
        release.set()
        w.join(timeout=30)
        runner.join(timeout=120)
        assert outcome == {"writer": "committed", "db": False}   # False: cancelled as stale, nothing deleted
        row = op_row(session, op.id)
        assert (row.status, row.error_code, row.database_cleaned_at) == ("CANCELLED", "preview_stale", None)
        session.rollback()
        assert session.execute(text("SELECT count(*) FROM laboratory_cleanup_object WHERE operation_id = :o"), {"o": str(op.id)}).scalar() == 0
        assert key in FakeS3Service.store and scoped_total_orders(session, world) > 0
        # a new preview now excludes the referenced object from the physical deletion
        assert key not in physical_keys(session, world)

    def test_a_writer_arriving_during_the_database_phase_waits_and_is_then_refused(self, session, world, monkeypatch):
        """Interleaving 2: the database phase holds the EXCLUSIVE asset lock; the writer waits and finds the key scheduled."""
        key = processed_key(world)
        bind = session.get_bind()
        op = queue_operation(session, world)
        claimed = service._claim(bind, op.id, "step-worker")
        service._phase_quiesce(bind, claimed, "step-worker")
        inside, proceed, outcome = threading.Event(), threading.Event(), {}
        real_delete = scope_mod.delete_graph

        def slow_delete(session_, scope, **kwargs):
            inside.set()
            proceed.wait(timeout=60)
            return real_delete(session_, scope, **kwargs)

        monkeypatch.setattr(scope_mod, "delete_graph", slow_delete)
        runner = threading.Thread(target=lambda: outcome.setdefault("db", service._phase_database(bind, claimed, "step-worker")))
        runner.start()
        assert inside.wait(timeout=60)

        def writer():
            with Session(bind) as s:
                s.add(ReportTemplate(tenant_id=world.other.tenant.id, name="Plantilla tardía", template_json={"logo_key": key}))
                try:
                    s.flush()
                    s.commit()
                    outcome["writer"] = "committed"
                except HTTPException as exc:
                    s.rollback()
                    outcome["writer"] = exc.detail["code"]

        w = threading.Thread(target=writer)
        w.start()
        time.sleep(1.0)
        assert w.is_alive() and "writer" not in outcome, "the writer must wait for the database phase"
        proceed.set()
        runner.join(timeout=120)
        w.join(timeout=60)
        assert outcome["db"] is True
        assert outcome["writer"] == "asset_scheduled_for_deletion"
        service._phase_objects(bind, claimed, "step-worker")
        assert key not in FakeS3Service.store  # no reference exists: it is deleted, and nothing dangling remains
        session.rollback()
        assert session.execute(text("SELECT count(*) FROM report_template WHERE name = 'Plantilla tardía'")).scalar() == 0


# ----------------------------------------------------------------------------------------------------------- R3


def target_deliveries(session, world, status=None):
    session.rollback()
    sql = "SELECT count(*) FROM notification_delivery WHERE tenant_id = :t"
    if status:
        sql += " AND status = :s"
    return session.execute(text(sql), {"t": str(world.target.tenant.id), "s": status}).scalar()


class TestDeliveryClaimsAndTheBarrier:
    @staticmethod
    def pause_inside_claim(monkeypatch):
        """Let the claim take its tenants' shared locks and read the gate, then hold it before the UPDATE."""
        inside, release = threading.Event(), threading.Event()
        real = maintenance_guard.tenants_in_maintenance

        def paused(session_, tenant_ids):
            blocked = real(session_, tenant_ids)
            inside.set()
            release.wait(timeout=60)
            return blocked

        monkeypatch.setattr(maintenance_guard, "tenants_in_maintenance", paused)
        return inside, release

    def test_a_claim_started_before_the_gate_finishes_first_and_the_cleanup_then_refuses_it(self, session, world, monkeypatch):
        bind = session.get_bind()
        inside, release = self.pause_inside_claim(monkeypatch)
        outcome = {}

        def worker():
            with Session(bind) as s:
                outcome["claimed"] = notification_delivery.claim_pending_deliveries(s, now=datetime.utcnow() + timedelta(days=1), limit=100)

        w = threading.Thread(target=worker)
        w.start()
        assert inside.wait(timeout=30)
        op = queue_operation(session, world)
        claimed_op = service._claim(bind, op.id, "step-worker")
        quiesce = threading.Thread(target=lambda: outcome.setdefault("q", service._phase_quiesce(bind, claimed_op, "step-worker") or "done"))
        quiesce.start()
        time.sleep(1.0)
        assert quiesce.is_alive(), "the barrier must wait for the claim transaction"
        release.set()
        w.join(timeout=30)
        quiesce.join(timeout=60)
        mine = [d for d in outcome["claimed"] if str(d.tenant_id) == str(world.target.tenant.id)]
        assert mine and all(d.status == "SENDING" for d in mine)
        # the destructive phase now sees the in-flight sends and refuses, retryably, deleting nothing
        before = target_deliveries(session, world)
        with pytest.raises(service.CleanupError) as refused:
            service._phase_database(bind, claimed_op, "step-worker")
        assert refused.value.code == "deliveries_in_flight"
        assert op_row(session, op.id).database_cleaned_at is None
        assert target_deliveries(session, world) == before and target_deliveries(session, world, "SENDING") == len(mine)

    def test_a_claim_after_the_barrier_skips_the_tenant_and_still_serves_the_others(self, session, world):
        bind = session.get_bind()
        op = queue_operation(session, world)  # the gate is up and the writer barrier has completed
        claimed_op = service._claim(bind, op.id, "step-worker")
        service._phase_quiesce(bind, claimed_op, "step-worker")
        with Session(bind) as s:
            claimed = notification_delivery.claim_pending_deliveries(s, now=datetime.utcnow() + timedelta(days=1), limit=100)
        tenants = {str(d.tenant_id) for d in claimed}
        assert str(world.target.tenant.id) not in tenants
        assert str(world.other.tenant.id) in tenants, "other laboratories keep being served"
        assert target_deliveries(session, world, "SENDING") == 0

    def test_no_obsolete_email_is_sent_for_cleaned_content_when_the_worker_resumes_after_the_cleanup(self, session, world):
        run_until_objects(session, world)
        assert target_deliveries(session, world) == 0, "pending deliveries of cleaned content are deleted with it"
        with Session(session.get_bind()) as s:
            claimed = notification_delivery.claim_pending_deliveries(s, now=datetime.utcnow() + timedelta(days=1), limit=100)
        assert str(world.target.tenant.id) not in {str(d.tenant_id) for d in claimed}


class TestUsageReconciliationAndTheBarrier:
    def test_a_run_committed_before_the_gate_blocks_the_preview_and_the_confirmation(self, session, world):
        tenant = world.target.tenant.id
        with Session(session.get_bind()) as s:
            s.execute(text("INSERT INTO tenant_usage_reconciliation (id, tenant_id, status, started_at) "
                           "VALUES (gen_random_uuid(), :t, 'RUNNING', now())"), {"t": str(tenant)})
            s.commit()
        with pytest.raises(HTTPException) as exc:
            queue_operation(session, world)
        assert exc.value.status_code == 409
        assert [b["code"] for b in exc.value.detail["blockers"]] == ["reconciliation_running"]

    def test_a_run_that_slipped_in_after_the_confirmation_makes_the_database_phase_refuse_and_retry_later(self, session, world):
        bind = session.get_bind()
        tenant = world.target.tenant.id
        op = queue_operation(session, world)
        claimed = service._claim(bind, op.id, "step-worker")
        service._phase_quiesce(bind, claimed, "step-worker")
        with Session(bind) as s:  # a run that was already past its gate check when the barrier completed
            s.execute(text("INSERT INTO tenant_usage_reconciliation (id, tenant_id, status, started_at) "
                           "VALUES (gen_random_uuid(), :t, 'RUNNING', now())"), {"t": str(tenant)})
            s.commit()
        with pytest.raises(service.CleanupError) as refused:
            service._phase_database(bind, claimed, "step-worker")
        assert refused.value.code == "reconciliation_running" and refused.value.retryable
        assert op_row(session, op.id).database_cleaned_at is None
        assert scoped_total_orders(session, world) > 0

    def test_a_run_that_checks_while_the_barrier_is_up_is_refused_and_never_starts(self, session, world):
        bind = session.get_bind()
        tenant = world.target.tenant.id
        op = queue_operation(session, world)
        claimed = service._claim(bind, op.id, "step-worker")
        service._phase_quiesce(bind, claimed, "step-worker")
        with Session(bind) as s:
            with pytest.raises(ConcurrentReconciliationError):
                UsageReconciliationService(s3=FakeS3Service()).reconcile_tenant(s, tenant)
        session.rollback()
        assert session.execute(text("SELECT count(*) FROM tenant_usage_reconciliation WHERE tenant_id = :t AND status = 'RUNNING'"),
                               {"t": str(tenant)}).scalar() == 0

    def test_a_run_waits_for_the_barrier_instead_of_slipping_between_its_check_and_its_start(self, session, world, monkeypatch):
        """The start of a reconciliation takes the tenant's SHARED lock: a barrier arriving in between waits for it."""
        bind = session.get_bind()
        tenant = world.target.tenant.id
        inside, release, outcome = threading.Event(), threading.Event(), {}
        real = maintenance_guard.tenants_in_maintenance

        def paused(session_, tenant_ids):
            blocked = real(session_, tenant_ids)
            inside.set()
            release.wait(timeout=60)
            return blocked

        monkeypatch.setattr("app.services.usage_reconciliation.tenants_in_maintenance", paused)

        def reconcile():
            with Session(bind) as s:
                try:
                    outcome["run"] = UsageReconciliationService(s3=FakeS3Service()).reconcile_tenant(s, tenant)
                except Exception as exc:  # the run itself may fail on the synthetic data; only the ordering matters here
                    outcome["run"] = type(exc).__name__

        r = threading.Thread(target=reconcile)
        r.start()
        assert inside.wait(timeout=30)
        op = queue_operation(session, world)
        claimed = service._claim(bind, op.id, "step-worker")
        quiesce = threading.Thread(target=lambda: service._phase_quiesce(bind, claimed, "step-worker"))
        quiesce.start()
        time.sleep(1.0)
        assert quiesce.is_alive(), "the barrier must wait for the reconciliation's start"
        release.set()
        r.join(timeout=120)
        quiesce.join(timeout=120)
        assert "run" in outcome
