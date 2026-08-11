"""Usage domain model and constraint tests (Céluma 1.3, Phase 4, Block B).

These run against the real migrated PostgreSQL schema (the `session` fixture
in conftest.py builds it with `alembic upgrade head`), not SQLModel metadata
alone — what is under test is the database constraints (CHECK, PK, FK), same
discipline `test_notification_models.py` established for the notification
domain.
"""
import uuid
from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.models.tenant_limits import TenantLimits
from app.models.tenant_usage import TenantUsage
from app.models.tenant_usage_reconciliation import (
    TenantUsageReconciliation,
    TenantUsageReconciliationStatus,
)
from tests.http.factories import create_tenant


class TestTenantUsage:
    def test_persists_with_defaults(self, session):
        tenant = create_tenant(session, name="Tenant A")

        usage = TenantUsage(tenant_id=tenant.id)
        session.add(usage)
        session.commit()
        session.refresh(usage)

        assert usage.tenant_id == tenant.id
        assert usage.billable_storage_bytes == 0
        assert isinstance(usage.last_updated, datetime)

    def test_at_most_one_row_per_tenant(self, session):
        tenant = create_tenant(session, name="Tenant A")
        session.add(TenantUsage(tenant_id=tenant.id))
        session.commit()

        with pytest.raises(IntegrityError):
            session.add(TenantUsage(tenant_id=tenant.id))
            session.commit()
        session.rollback()

    def test_negative_storage_is_rejected(self, session):
        tenant = create_tenant(session, name="Tenant A")

        with pytest.raises(IntegrityError):
            session.add(
                TenantUsage(tenant_id=tenant.id, billable_storage_bytes=-1)
            )
            session.commit()
        session.rollback()

    def test_storage_supports_values_beyond_32_bit_range(self, session):
        """BIGINT, not INTEGER — storage usage/limits can exceed 2GB."""
        tenant = create_tenant(session, name="Tenant A")
        beyond_32_bit = 2**31 + 1024  # just past a signed 32-bit INTEGER's max

        session.add(
            TenantUsage(tenant_id=tenant.id, billable_storage_bytes=beyond_32_bit)
        )
        session.commit()

        stored = session.get(TenantUsage, tenant.id)
        assert stored.billable_storage_bytes == beyond_32_bit

    def test_absence_of_a_row_means_uninitialized_not_zero(self, session):
        """No TenantUsage row exists for a tenant that was never given one —
        confirming there is nothing to accidentally read as a zero-usage
        row via a plain session.get()."""
        tenant = create_tenant(session, name="Tenant A")
        assert session.get(TenantUsage, tenant.id) is None


class TestTenantLimits:
    def test_persists_with_null_limits(self, session):
        tenant = create_tenant(session, name="Tenant A")

        limits = TenantLimits(tenant_id=tenant.id)
        session.add(limits)
        session.commit()
        session.refresh(limits)

        assert limits.storage_limit_bytes is None
        assert limits.user_limit is None
        assert isinstance(limits.updated_at, datetime)

    def test_persists_with_positive_limits(self, session):
        tenant = create_tenant(session, name="Tenant A")

        limits = TenantLimits(
            tenant_id=tenant.id, storage_limit_bytes=1024, user_limit=5
        )
        session.add(limits)
        session.commit()
        session.refresh(limits)

        assert limits.storage_limit_bytes == 1024
        assert limits.user_limit == 5

    def test_at_most_one_row_per_tenant(self, session):
        tenant = create_tenant(session, name="Tenant A")
        session.add(TenantLimits(tenant_id=tenant.id))
        session.commit()

        with pytest.raises(IntegrityError):
            session.add(TenantLimits(tenant_id=tenant.id))
            session.commit()
        session.rollback()

    @pytest.mark.parametrize("bad_storage_limit", [0, -1])
    def test_zero_and_negative_storage_limit_rejected(self, session, bad_storage_limit):
        tenant = create_tenant(session, name="Tenant A")

        with pytest.raises(IntegrityError):
            session.add(
                TenantLimits(tenant_id=tenant.id, storage_limit_bytes=bad_storage_limit)
            )
            session.commit()
        session.rollback()

    @pytest.mark.parametrize("bad_user_limit", [0, -1])
    def test_zero_and_negative_user_limit_rejected(self, session, bad_user_limit):
        tenant = create_tenant(session, name="Tenant A")

        with pytest.raises(IntegrityError):
            session.add(TenantLimits(tenant_id=tenant.id, user_limit=bad_user_limit))
            session.commit()
        session.rollback()

    def test_absence_of_a_row_means_no_limits_configured(self, session):
        tenant = create_tenant(session, name="Tenant A")
        assert session.get(TenantLimits, tenant.id) is None


class TestTenantUsageReconciliation:
    def test_running_row_has_no_completed_at(self, session):
        tenant = create_tenant(session, name="Tenant A")

        run = TenantUsageReconciliation(tenant_id=tenant.id)
        session.add(run)
        session.commit()
        session.refresh(run)

        assert run.status == TenantUsageReconciliationStatus.RUNNING
        assert run.completed_at is None

    def test_running_row_with_completed_at_is_rejected(self, session):
        tenant = create_tenant(session, name="Tenant A")

        with pytest.raises(IntegrityError):
            session.add(
                TenantUsageReconciliation(
                    tenant_id=tenant.id,
                    status=TenantUsageReconciliationStatus.RUNNING,
                    completed_at=datetime.utcnow(),
                )
            )
            session.commit()
        session.rollback()

    def test_succeeded_row_requires_completed_at(self, session):
        tenant = create_tenant(session, name="Tenant A")

        with pytest.raises(IntegrityError):
            session.add(
                TenantUsageReconciliation(
                    tenant_id=tenant.id,
                    status=TenantUsageReconciliationStatus.SUCCEEDED,
                    completed_at=None,
                )
            )
            session.commit()
        session.rollback()

    def test_failed_row_requires_completed_at(self, session):
        tenant = create_tenant(session, name="Tenant A")

        with pytest.raises(IntegrityError):
            session.add(
                TenantUsageReconciliation(
                    tenant_id=tenant.id,
                    status=TenantUsageReconciliationStatus.FAILED,
                    completed_at=None,
                )
            )
            session.commit()
        session.rollback()

    def test_succeeded_row_with_error_code_is_rejected(self, session):
        tenant = create_tenant(session, name="Tenant A")

        with pytest.raises(IntegrityError):
            session.add(
                TenantUsageReconciliation(
                    tenant_id=tenant.id,
                    status=TenantUsageReconciliationStatus.SUCCEEDED,
                    completed_at=datetime.utcnow(),
                    error_code="unexpected",
                )
            )
            session.commit()
        session.rollback()

    def test_failed_row_may_carry_a_sanitized_error_code(self, session):
        tenant = create_tenant(session, name="Tenant A")

        run = TenantUsageReconciliation(
            tenant_id=tenant.id,
            status=TenantUsageReconciliationStatus.FAILED,
            completed_at=datetime.utcnow(),
            error_code="s3_timeout",
        )
        session.add(run)
        session.commit()
        session.refresh(run)

        assert run.error_code == "s3_timeout"

    @pytest.mark.parametrize(
        "field", ["objects_checked", "orphans_found", "missing_objects_found"]
    )
    def test_negative_counters_are_rejected(self, session, field):
        tenant = create_tenant(session, name="Tenant A")

        with pytest.raises(IntegrityError):
            session.add(
                TenantUsageReconciliation(
                    tenant_id=tenant.id,
                    status=TenantUsageReconciliationStatus.SUCCEEDED,
                    completed_at=datetime.utcnow(),
                    **{field: -1},
                )
            )
            session.commit()
        session.rollback()

    def test_a_tenant_may_have_many_reconciliation_rows(self, session):
        """Append-only history, not a one-row-per-tenant table like
        TenantUsage/TenantLimits."""
        tenant = create_tenant(session, name="Tenant A")

        for _ in range(3):
            session.add(TenantUsageReconciliation(tenant_id=tenant.id))
        session.commit()

        rows = session.exec(
            select(TenantUsageReconciliation).where(
                TenantUsageReconciliation.tenant_id == tenant.id
            )
        ).all()
        assert len(rows) == 3

    def test_difference_bytes_is_actual_minus_expected(self, session):
        """Ratified convention: actual - expected. Positive means the
        counter under-counted; negative means it over-counted."""
        tenant = create_tenant(session, name="Tenant A")

        run = TenantUsageReconciliation(
            tenant_id=tenant.id,
            status=TenantUsageReconciliationStatus.SUCCEEDED,
            completed_at=datetime.utcnow(),
            expected_storage_bytes=1000,
            actual_storage_bytes=1200,
            difference_bytes=200,
        )
        session.add(run)
        session.commit()
        session.refresh(run)

        assert run.difference_bytes == run.actual_storage_bytes - run.expected_storage_bytes
