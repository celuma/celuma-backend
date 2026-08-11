"""UsageService read-path tests (Céluma 1.3, Phase 4, Block B) plus the
Block C write API (`initialize_usage`, `adjust_storage`/`increment_storage`/
`decrement_storage`, `record_storage_delta`).

Covers the contract in docs/celuma-1.3/phase-4-block-b/usage-service-
contract.md: missing-row semantics for `get_usage`/`get_limits`, latest-
reconciliation ordering, and tenant isolation across all four methods.
`get_user_metrics`'s own exact-value fixture lives in
test_tenant_user_metrics.py; this file exercises it only for tenant
isolation, not the full counting matrix.

The write-API tests below cover docs/celuma-1.3/phase-4-block-c/
incremental-usage-accounting-contract.md: atomic delta semantics, the
non-negative floor, missing-usage behavior (`UsageNotInitializedError` vs.
the failure-contained `record_storage_delta`), and initialization
idempotency.
"""
import threading
from datetime import datetime, timedelta

import pytest
from sqlmodel import Session

from app.models.tenant_limits import TenantLimits
from app.models.tenant_usage import TenantUsage
from app.models.tenant_usage_reconciliation import (
    TenantUsageReconciliation,
    TenantUsageReconciliationStatus,
)
from app.services.usage import UsageNotInitializedError, UsageService
from tests.http.factories import create_tenant, create_user


class TestGetUsage:
    def test_returns_none_when_uninitialized(self, session):
        tenant = create_tenant(session, name="Tenant A")
        assert UsageService.get_usage(session, tenant.id) is None

    def test_returns_the_row_once_initialized(self, session):
        tenant = create_tenant(session, name="Tenant A")
        session.add(TenantUsage(tenant_id=tenant.id, billable_storage_bytes=4096))
        session.commit()

        usage = UsageService.get_usage(session, tenant.id)
        assert usage is not None
        assert usage.billable_storage_bytes == 4096

    def test_zero_initialized_usage_is_distinct_from_uninitialized(self, session):
        """A row explicitly at zero is not None — the row's existence, not
        its value, is what "initialized" means."""
        tenant = create_tenant(session, name="Tenant A")
        session.add(TenantUsage(tenant_id=tenant.id, billable_storage_bytes=0))
        session.commit()

        usage = UsageService.get_usage(session, tenant.id)
        assert usage is not None
        assert usage.billable_storage_bytes == 0

    def test_tenant_isolation(self, session):
        tenant_a = create_tenant(session, name="Tenant A")
        tenant_b = create_tenant(session, name="Tenant B")
        session.add(TenantUsage(tenant_id=tenant_a.id, billable_storage_bytes=111))
        session.add(TenantUsage(tenant_id=tenant_b.id, billable_storage_bytes=222))
        session.commit()

        assert UsageService.get_usage(session, tenant_a.id).billable_storage_bytes == 111
        assert UsageService.get_usage(session, tenant_b.id).billable_storage_bytes == 222


class TestGetLimits:
    def test_returns_none_when_not_configured(self, session):
        tenant = create_tenant(session, name="Tenant A")
        assert UsageService.get_limits(session, tenant.id) is None

    def test_returns_the_row_with_unlimited_nulls(self, session):
        tenant = create_tenant(session, name="Tenant A")
        session.add(TenantLimits(tenant_id=tenant.id))
        session.commit()

        limits = UsageService.get_limits(session, tenant.id)
        assert limits is not None
        assert limits.storage_limit_bytes is None
        assert limits.user_limit is None

    def test_returns_the_row_with_configured_limits(self, session):
        tenant = create_tenant(session, name="Tenant A")
        session.add(
            TenantLimits(tenant_id=tenant.id, storage_limit_bytes=10_000, user_limit=25)
        )
        session.commit()

        limits = UsageService.get_limits(session, tenant.id)
        assert limits.storage_limit_bytes == 10_000
        assert limits.user_limit == 25

    def test_tenant_isolation(self, session):
        tenant_a = create_tenant(session, name="Tenant A")
        tenant_b = create_tenant(session, name="Tenant B")
        session.add(TenantLimits(tenant_id=tenant_a.id, user_limit=5))
        session.add(TenantLimits(tenant_id=tenant_b.id, user_limit=50))
        session.commit()

        assert UsageService.get_limits(session, tenant_a.id).user_limit == 5
        assert UsageService.get_limits(session, tenant_b.id).user_limit == 50


class TestGetLatestReconciliation:
    def test_returns_none_when_never_run(self, session):
        tenant = create_tenant(session, name="Tenant A")
        assert UsageService.get_latest_reconciliation(session, tenant.id) is None

    def test_returns_the_most_recently_started_run(self, session):
        tenant = create_tenant(session, name="Tenant A")
        now = datetime.utcnow()

        older = TenantUsageReconciliation(
            tenant_id=tenant.id,
            status=TenantUsageReconciliationStatus.SUCCEEDED,
            started_at=now - timedelta(hours=2),
            completed_at=now - timedelta(hours=1, minutes=59),
        )
        newer = TenantUsageReconciliation(
            tenant_id=tenant.id,
            status=TenantUsageReconciliationStatus.SUCCEEDED,
            started_at=now - timedelta(minutes=5),
            completed_at=now - timedelta(minutes=4),
        )
        session.add(older)
        session.add(newer)
        session.commit()
        session.refresh(newer)

        latest = UsageService.get_latest_reconciliation(session, tenant.id)
        assert latest.id == newer.id

    def test_a_failed_run_can_be_the_latest(self, session):
        """History includes failures — the service does not filter them
        out or prefer a successful run over a more recent failed one."""
        tenant = create_tenant(session, name="Tenant A")
        now = datetime.utcnow()

        succeeded = TenantUsageReconciliation(
            tenant_id=tenant.id,
            status=TenantUsageReconciliationStatus.SUCCEEDED,
            started_at=now - timedelta(hours=1),
            completed_at=now - timedelta(minutes=59),
        )
        failed = TenantUsageReconciliation(
            tenant_id=tenant.id,
            status=TenantUsageReconciliationStatus.FAILED,
            started_at=now - timedelta(minutes=10),
            completed_at=now - timedelta(minutes=9),
            error_code="s3_timeout",
        )
        session.add(succeeded)
        session.add(failed)
        session.commit()
        session.refresh(failed)

        latest = UsageService.get_latest_reconciliation(session, tenant.id)
        assert latest.id == failed.id
        assert latest.status == TenantUsageReconciliationStatus.FAILED

    def test_a_running_run_can_be_the_latest(self, session):
        tenant = create_tenant(session, name="Tenant A")
        run = TenantUsageReconciliation(tenant_id=tenant.id)
        session.add(run)
        session.commit()
        session.refresh(run)

        latest = UsageService.get_latest_reconciliation(session, tenant.id)
        assert latest.id == run.id
        assert latest.status == TenantUsageReconciliationStatus.RUNNING

    def test_tenant_isolation(self, session):
        tenant_a = create_tenant(session, name="Tenant A")
        tenant_b = create_tenant(session, name="Tenant B")
        run_a = TenantUsageReconciliation(tenant_id=tenant_a.id)
        run_b = TenantUsageReconciliation(tenant_id=tenant_b.id)
        session.add(run_a)
        session.add(run_b)
        session.commit()
        session.refresh(run_a)
        session.refresh(run_b)

        assert UsageService.get_latest_reconciliation(session, tenant_a.id).id == run_a.id
        assert UsageService.get_latest_reconciliation(session, tenant_b.id).id == run_b.id


class TestGetUserMetricsTenantIsolation:
    def test_tenant_a_never_sees_tenant_b_users(self, session):
        tenant_a = create_tenant(session, name="Tenant A")
        tenant_b = create_tenant(session, name="Tenant B")
        create_user(session, tenant_a, email="a1@a.test", roles=("admin",))
        create_user(session, tenant_b, email="b1@b.test", roles=("admin",))
        create_user(session, tenant_b, email="b2@b.test", roles=("admin",))

        metrics_a = UsageService.get_user_metrics(session, tenant_a.id)
        metrics_b = UsageService.get_user_metrics(session, tenant_b.id)

        assert metrics_a.registered_users == 1
        assert metrics_a.active_internal_users == 1
        assert metrics_b.registered_users == 2
        assert metrics_b.active_internal_users == 2


class TestInitializeUsage:
    def test_creates_a_row_with_the_given_baseline(self, session):
        tenant = create_tenant(session, name="Tenant A")
        UsageService.initialize_usage(session, tenant.id, billable_storage_bytes=4096)
        session.commit()

        usage = UsageService.get_usage(session, tenant.id)
        assert usage is not None
        assert usage.billable_storage_bytes == 4096

    def test_zero_baseline_is_a_real_initialized_row(self, session):
        tenant = create_tenant(session, name="Tenant C")
        UsageService.initialize_usage(session, tenant.id, billable_storage_bytes=0)
        session.commit()

        usage = UsageService.get_usage(session, tenant.id)
        assert usage is not None
        assert usage.billable_storage_bytes == 0

    def test_running_twice_does_not_change_an_already_initialized_row(self, session):
        """Idempotent by construction: a second call must never overwrite or
        recompute an existing row, even with a different baseline value."""
        tenant = create_tenant(session, name="Tenant A")
        UsageService.initialize_usage(session, tenant.id, billable_storage_bytes=1000)
        session.commit()

        UsageService.initialize_usage(session, tenant.id, billable_storage_bytes=999_999)
        session.commit()

        usage = UsageService.get_usage(session, tenant.id)
        assert usage.billable_storage_bytes == 1000

    def test_never_implemented_as_additive_replay(self, session):
        """Explicit anti-regression for the master spec's 'not current +=
        historical_sum' instruction: two initialize_usage calls for the same
        tenant must not sum their baselines."""
        tenant = create_tenant(session, name="Tenant A")
        UsageService.initialize_usage(session, tenant.id, billable_storage_bytes=500)
        session.commit()
        UsageService.initialize_usage(session, tenant.id, billable_storage_bytes=500)
        session.commit()

        usage = UsageService.get_usage(session, tenant.id)
        assert usage.billable_storage_bytes == 500  # not 1000


class TestAdjustStorage:
    def test_increment_storage(self, session):
        tenant = create_tenant(session, name="Tenant A")
        UsageService.initialize_usage(session, tenant.id, billable_storage_bytes=0)
        session.commit()

        new_total = UsageService.increment_storage(
            session, tenant.id, 1024, source="test"
        )
        session.commit()
        assert new_total == 1024
        assert UsageService.get_usage(session, tenant.id).billable_storage_bytes == 1024

    def test_decrement_storage(self, session):
        tenant = create_tenant(session, name="Tenant A")
        UsageService.initialize_usage(session, tenant.id, billable_storage_bytes=1000)
        session.commit()

        new_total = UsageService.decrement_storage(session, tenant.id, 400, source="test")
        session.commit()
        assert new_total == 600

    def test_decrement_floors_at_zero_not_negative(self, session):
        tenant = create_tenant(session, name="Tenant A")
        UsageService.initialize_usage(session, tenant.id, billable_storage_bytes=100)
        session.commit()

        new_total = UsageService.decrement_storage(session, tenant.id, 500, source="test")
        session.commit()
        assert new_total == 0
        assert UsageService.get_usage(session, tenant.id).billable_storage_bytes == 0

    def test_large_bigint_values(self, session):
        tenant = create_tenant(session, name="Tenant A")
        big = 5 * 1024 * 1024 * 1024 * 1024  # 5 TB, well past int32
        UsageService.initialize_usage(session, tenant.id, billable_storage_bytes=big)
        session.commit()

        new_total = UsageService.increment_storage(session, tenant.id, big, source="test")
        session.commit()
        assert new_total == big * 2

    def test_missing_usage_raises_usage_not_initialized_error(self, session):
        tenant = create_tenant(session, name="Tenant A")
        with pytest.raises(UsageNotInitializedError):
            UsageService.adjust_storage(session, tenant.id, 100, source="test")

    def test_adjust_storage_never_creates_a_row(self, session):
        tenant = create_tenant(session, name="Tenant A")
        with pytest.raises(UsageNotInitializedError):
            UsageService.adjust_storage(session, tenant.id, 100, source="test")
        session.rollback()
        assert UsageService.get_usage(session, tenant.id) is None


class TestRecordStorageDelta:
    """The failure-contained wrapper write-flow call sites use."""

    def test_applies_the_delta_when_initialized(self, session):
        tenant = create_tenant(session, name="Tenant A")
        UsageService.initialize_usage(session, tenant.id, billable_storage_bytes=0)
        session.commit()

        UsageService.record_storage_delta(session, tenant.id, 2048, source="test")
        session.commit()
        assert UsageService.get_usage(session, tenant.id).billable_storage_bytes == 2048

    def test_never_raises_when_uninitialized(self, session):
        tenant = create_tenant(session, name="Tenant A")
        # Must not raise — this is the whole point of the wrapper.
        UsageService.record_storage_delta(session, tenant.id, 2048, source="test")
        session.commit()
        # And it must not have lazily created a row either.
        assert UsageService.get_usage(session, tenant.id) is None

    def test_zero_delta_is_a_no_op(self, session):
        tenant = create_tenant(session, name="Tenant A")
        UsageService.initialize_usage(session, tenant.id, billable_storage_bytes=50)
        session.commit()

        UsageService.record_storage_delta(session, tenant.id, 0, source="test")
        session.commit()
        assert UsageService.get_usage(session, tenant.id).billable_storage_bytes == 50

    def test_unexpected_database_error_is_not_swallowed(self, session):
        """Céluma 1.3 Phase 4, Block C remediation — clarifying the
        failure-containment contract (incremental-usage-accounting-
        contract.md §4): only `UsageNotInitializedError` is contained.
        Any other exception — here, a delta value the `BIGINT` column
        cannot hold, which Postgres itself rejects as an out-of-range
        error — must propagate. `record_storage_delta` must never become
        `except Exception: pass`."""
        tenant = create_tenant(session, name="Tenant A")
        UsageService.initialize_usage(session, tenant.id, billable_storage_bytes=0)
        session.commit()

        with pytest.raises(Exception) as exc_info:
            UsageService.record_storage_delta(
                session, tenant.id, 10**30, source="test"
            )
        assert not isinstance(exc_info.value, UsageNotInitializedError)
        session.rollback()

    def test_adjust_storage_propagates_the_same_unexpected_error(self, session):
        """The strict primitive must propagate it too — `record_storage_
        delta`'s containment is additive on top of `adjust_storage`, not a
        different code path with different behavior for non-initialization
        failures."""
        tenant = create_tenant(session, name="Tenant A")
        UsageService.initialize_usage(session, tenant.id, billable_storage_bytes=0)
        session.commit()

        with pytest.raises(Exception) as exc_info:
            UsageService.adjust_storage(session, tenant.id, 10**30, source="test")
        assert not isinstance(exc_info.value, UsageNotInitializedError)
        session.rollback()


class TestConcurrentAdjustments:
    """Real PostgreSQL concurrency — two separate connections/sessions
    against the same tenant_usage row, not mocked arithmetic."""

    def test_concurrent_increments_both_apply(self, engine):
        with Session(engine) as setup_session:
            tenant = create_tenant(setup_session, name="Concurrent A")
            tenant_id = tenant.id
            UsageService.initialize_usage(setup_session, tenant_id, billable_storage_bytes=0)
            setup_session.commit()

        errors = []

        def _increment():
            try:
                with Session(engine) as s:
                    UsageService.increment_storage(s, tenant_id, 100, source="test")
                    s.commit()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_increment) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        with Session(engine) as verify_session:
            usage = UsageService.get_usage(verify_session, tenant_id)
            assert usage.billable_storage_bytes == 1000

    def test_concurrent_increment_and_decrement_serialize_correctly(self, engine):
        with Session(engine) as setup_session:
            tenant = create_tenant(setup_session, name="Concurrent B")
            tenant_id = tenant.id
            UsageService.initialize_usage(setup_session, tenant_id, billable_storage_bytes=1000)
            setup_session.commit()

        errors = []

        def _increment():
            try:
                with Session(engine) as s:
                    UsageService.increment_storage(s, tenant_id, 250, source="test")
                    s.commit()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def _decrement():
            try:
                with Session(engine) as s:
                    UsageService.decrement_storage(s, tenant_id, 250, source="test")
                    s.commit()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = (
            [threading.Thread(target=_increment) for _ in range(5)]
            + [threading.Thread(target=_decrement) for _ in range(5)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        with Session(engine) as verify_session:
            usage = UsageService.get_usage(verify_session, tenant_id)
            assert usage.billable_storage_bytes == 1000  # net zero change
