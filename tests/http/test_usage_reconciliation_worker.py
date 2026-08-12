"""Reconciliation worker tests (Céluma 1.3, Phase 4, Block D).

Covers reconciliation-worker-contract.md: default-off gating, per-tenant
containment (one tenant's failure must not cost the others their cycle),
active-tenant selection, stale recovery on every cycle, and the lifecycle
wiring. The loop body is driven synchronously — same approach as
`test_notification_delivery_worker.py`, no event loop and no sleeping.
"""
import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from app.core.config import settings
from app.models.storage import StorageObject
from app.models.tenant_usage import TenantUsage
from app.models.tenant_usage_reconciliation import (
    TenantUsageReconciliation,
    TenantUsageReconciliationStatus,
)
from app.services.usage import UsageService
from app.services.usage_reconciliation import UsageReconciliationService
from app.services.usage_reconciliation_worker import (
    UsageReconciliationWorker,
    run_reconciliation_cycle,
    start_reconciliation_worker,
    stop_reconciliation_worker,
)
from tests.http.conftest import FakeS3Service
from tests.http.factories import create_tenant


def _service():
    return UsageReconciliationService(s3=FakeS3Service())


def _init_usage(session, tenant_id, *, billable_storage_bytes=0):
    UsageService.initialize_usage(
        session, tenant_id, billable_storage_bytes=billable_storage_bytes
    )
    session.commit()


def _billable_object(session, tenant, *, size_bytes):
    key = f"reports/{tenant.id}/{uuid.uuid4().hex}/official/{uuid.uuid4().hex}.pdf"
    obj = StorageObject(
        provider="aws",
        region="mx-test-1",
        bucket="celuma-test-bucket",
        object_key=key,
        content_type="application/pdf",
        size_bytes=size_bytes,
        sha256_hex=uuid.uuid4().hex,
        etag="fake-etag",
        tenant_id=tenant.id,
    )
    session.add(obj)
    session.commit()
    FakeS3Service.put_raw(key, b"x" * size_bytes)
    return obj


class TestCycle:
    def test_every_active_tenant_is_reconciled(self, session):
        tenant_a = create_tenant(session, name="A")
        tenant_b = create_tenant(session, name="B")
        _billable_object(session, tenant_a, size_bytes=100)
        _billable_object(session, tenant_b, size_bytes=200)
        _init_usage(session, tenant_a.id)
        _init_usage(session, tenant_b.id)

        result = run_reconciliation_cycle(session, _service())

        assert result.tenants == 2
        assert result.succeeded == 2
        assert session.get(TenantUsage, tenant_a.id).billable_storage_bytes == 100
        assert session.get(TenantUsage, tenant_b.id).billable_storage_bytes == 200

    def test_inactive_tenants_are_skipped(self, session):
        active = create_tenant(session, name="Active")
        inactive = create_tenant(session, name="Inactive")
        inactive.is_active = False
        session.add(inactive)
        session.commit()
        _init_usage(session, active.id)
        _init_usage(session, inactive.id, billable_storage_bytes=999)
        _billable_object(session, inactive, size_bytes=5)

        result = run_reconciliation_cycle(session, _service())

        assert result.tenants == 1
        assert session.get(TenantUsage, inactive.id).billable_storage_bytes == 999

    def test_one_tenant_failing_does_not_stop_the_others(self, session):
        first = create_tenant(session, name="AAA")
        second = create_tenant(session, name="BBB")
        _billable_object(session, first, size_bytes=50)
        _billable_object(session, second, size_bytes=60)
        _init_usage(session, first.id)
        _init_usage(session, second.id)

        failing_tenant = {"id": None}

        class PartlyBrokenService(UsageReconciliationService):
            def reconcile_tenant(self, session, tenant_id, **kwargs):
                if failing_tenant["id"] is None:
                    failing_tenant["id"] = tenant_id
                    raise RuntimeError("simulated unexpected failure")
                return super().reconcile_tenant(session, tenant_id, **kwargs)

        result = run_reconciliation_cycle(
            session, PartlyBrokenService(s3=FakeS3Service())
        )

        assert result.tenants == 2
        assert result.failed == 1
        assert result.succeeded == 1
        survivor = second.id if failing_tenant["id"] == first.id else first.id
        expected = 60 if survivor == second.id else 50
        assert session.get(TenantUsage, survivor).billable_storage_bytes == expected

    def test_a_cycle_recovers_stale_runs_first(self, session):
        tenant = create_tenant(session)
        _init_usage(session, tenant.id)
        stale = TenantUsageReconciliation(
            tenant_id=tenant.id,
            status=TenantUsageReconciliationStatus.RUNNING,
            started_at=datetime.utcnow()
            - timedelta(seconds=settings.usage_reconciliation_stale_seconds + 60),
        )
        session.add(stale)
        session.commit()

        result = run_reconciliation_cycle(session, _service())

        assert result.recovered == 1
        session.refresh(stale)
        assert stale.status == TenantUsageReconciliationStatus.FAILED
        assert stale.error_code == "stale_run_recovered"
        # And the tenant was still reconciled in the same cycle — recovery
        # is what unblocks it.
        assert result.succeeded == 1

    def test_a_tenant_already_running_is_skipped_not_failed(self, session):
        tenant = create_tenant(session)
        _init_usage(session, tenant.id)
        session.add(
            TenantUsageReconciliation(
                tenant_id=tenant.id,
                status=TenantUsageReconciliationStatus.RUNNING,
                started_at=datetime.utcnow(),
            )
        )
        session.commit()

        result = run_reconciliation_cycle(session, _service())

        assert result.skipped == 1
        assert result.failed == 0

    def test_shutdown_stops_the_cycle_between_tenants(self, session):
        for index in range(3):
            tenant = create_tenant(session, name=f"T{index}")
            _init_usage(session, tenant.id)

        result = run_reconciliation_cycle(
            session, _service(), should_stop=lambda: True
        )

        assert result.tenants == 3
        assert result.succeeded == 0
        assert (
            session.exec(select(TenantUsageReconciliation)).all() == []
        ), "no run should have started"


class TestWorkerLifecycle:
    """The asyncio loop, driven with `asyncio.run` — the same convention
    `test_notification_delivery_worker.py` uses, so these tests need no
    async plugin."""

    def test_disabled_by_default(self):
        assert settings.usage_reconciliation_enabled is False

    def test_it_does_not_start_while_reconciliation_is_disabled(self):
        assert asyncio.run(start_reconciliation_worker()) is None
        asyncio.run(stop_reconciliation_worker())

    def test_it_starts_when_explicitly_enabled(self, monkeypatch, engine):
        monkeypatch.setattr(settings, "usage_reconciliation_enabled", True)
        monkeypatch.setattr(settings, "usage_reconciliation_interval_seconds", 3600)

        async def run():
            worker = await start_reconciliation_worker()
            assert worker is not None and worker.running
            await stop_reconciliation_worker()
            return worker

        worker = asyncio.run(run())
        assert worker.running is False

    def test_starting_twice_raises_rather_than_doubling_the_s3_traffic(self):
        async def run():
            worker = UsageReconciliationWorker(
                interval_seconds=3600, session_factory=lambda: _NullSession()
            )
            await worker.start()
            try:
                with pytest.raises(RuntimeError):
                    await worker.start()
            finally:
                await worker.stop()
            return worker

        assert asyncio.run(run()).running is False

    def test_stopping_a_worker_that_never_started_is_a_no_op(self):
        asyncio.run(UsageReconciliationWorker(interval_seconds=3600).stop())

    def test_shutdown_does_not_wait_out_the_poll_interval(self):
        """The sleep between cycles waits on the stop event, not on
        `asyncio.sleep(interval)` — otherwise every deploy would hang for
        up to six hours."""

        async def run():
            worker = UsageReconciliationWorker(
                interval_seconds=3600, session_factory=lambda: _NullSession()
            )
            await worker.start()
            await asyncio.sleep(0.05)
            started = asyncio.get_event_loop().time()
            await worker.stop()
            return asyncio.get_event_loop().time() - started

        assert asyncio.run(run()) < 2.0

    def test_the_interval_default_is_hours_not_seconds(self):
        """A reconciliation cycle HEADs every billable object of every
        active tenant; a delivery-worker-style 10s interval would be an S3
        bill, not a safety net."""
        assert settings.usage_reconciliation_interval_seconds >= 3600


class _NullSession:
    """A session stand-in for the lifecycle tests, which exercise the loop's
    start/stop behavior and must not touch a database from a worker
    thread."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def exec(self, *args, **kwargs):
        raise RuntimeError("no database in the lifecycle tests")
