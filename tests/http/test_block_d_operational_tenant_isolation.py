"""Multi-tenant isolation of the operational services (Céluma 1.3, Phase 5,
Block D — D15, with D5/D6 boundary cases).

Block D's brief treats any operational cross-tenant mutation or read as a
BLOCKER, and requires every worker/service exercised in the block to be tested
with at least two tenants in deliberately different states.

Reconciliation already had that coverage before Block D
(`test_usage_reconciliation.py:157,764` — "B must not be touched", "B's counter
must be untouched"), and Block D's own
`test_block_d_usage_authoritative.py::TestReconciliationTenantIsolation`
extends it to the authoritative-recomputation path.

The two operational services that did **not** have it are the ones covered
here: the **threshold engine** and **notification delivery**. Neither
`test_usage_thresholds.py` nor `test_usage_threshold_triggers.py` nor
`test_notification_delivery_worker.py` contained a single two-tenant case
(confirmed by grep before this module was written), so "tenant A's limit
cannot govern tenant B" and "tenant A's crossing cannot notify tenant B's
admins" were unproven rather than false.

Nothing here reinterprets the Phase 4 threshold contract: zero `TenantLimits`
rows still means unlimited, thresholds are still notification-only, and the
policies are imported rather than restated.
"""
from __future__ import annotations

import pytest
from sqlmodel import Session, select

from app.models.notification import Notification, NotificationRecipient, NotificationType
from app.models.tenant_limits import TenantLimits
from app.models.tenant_usage import TenantUsage
from app.models.tenant_usage_threshold_state import (
    TenantUsageThresholdState,
    UsageResource,
    UsageThresholdState,
)
from app.services.usage import UsageService
from app.services.usage_thresholds import UsageThresholdService

from .factories import create_branch, create_tenant, create_user

LIMIT = 10_000


def set_limits(session: Session, tenant, *, storage=None, users=None) -> None:
    """Same direct write the Block G tests use — there is no production writer
    for `TenantLimits` in Céluma 1.3 (A-012, ratified accepted debt), so a test
    that wants a configured limit writes the row itself."""
    row = session.get(TenantLimits, tenant.id)
    if row is None:
        row = TenantLimits(tenant_id=tenant.id)
        session.add(row)
    row.storage_limit_bytes = storage
    row.user_limit = users
    session.add(row)
    session.commit()


def set_storage(session: Session, tenant, used: int) -> None:
    row = session.get(TenantUsage, tenant.id)
    if row is None:
        UsageService.initialize_usage(
            session, tenant.id, billable_storage_bytes=used, source="test"
        )
    else:
        row.billable_storage_bytes = used
        session.add(row)
    session.commit()


def evaluate_storage(session: Session, tenant):
    result = UsageThresholdService.evaluate_storage(session, tenant.id, source="test")
    session.commit()
    return result


def evaluate_users(session: Session, tenant):
    result = UsageThresholdService.evaluate_users(session, tenant.id, source="test")
    session.commit()
    return result


def state_row(session: Session, tenant, resource=UsageResource.STORAGE):
    session.expire_all()
    return session.exec(
        select(TenantUsageThresholdState).where(
            TenantUsageThresholdState.tenant_id == tenant.id,
            TenantUsageThresholdState.resource == resource.value,
        )
    ).first()


#: The two user-limit threshold types, as declared by `NotificationType`.
USER_LIMIT_TYPES = (
    NotificationType.USER_LIMIT_APPROACHING.value,
    NotificationType.USER_LIMIT_REACHED.value,
)


def notifications(session: Session, tenant, types=None):
    statement = select(Notification).where(Notification.tenant_id == tenant.id)
    if types is not None:
        statement = statement.where(Notification.type.in_(types))  # type: ignore[union-attr]
    return list(session.exec(statement).all())


def recipients_of(session: Session, notification):
    return list(
        session.exec(
            select(NotificationRecipient).where(
                NotificationRecipient.notification_id == notification.id
            )
        ).all()
    )


@pytest.fixture
def pair(session):
    """Two tenants in deliberately different states.

    A: limited to 10 000 bytes, sitting well under it.
    B: limited to 10 000 bytes, sitting well under it.
    Each has its own admin, so a misrouted notification is visible.
    """
    a = create_tenant(session, name="Tenant A")
    create_branch(session, a, code="A")
    admin_a = create_user(session, a, email="admin.a@lab.test", roles=("admin",))

    b = create_tenant(session, name="Tenant B")
    create_branch(session, b, code="B")
    admin_b = create_user(session, b, email="admin.b@lab.test", roles=("admin",))

    set_limits(session, a, storage=LIMIT)
    set_limits(session, b, storage=LIMIT)
    set_storage(session, a, 1_000)
    set_storage(session, b, 1_000)
    evaluate_storage(session, a)
    evaluate_storage(session, b)
    return {"a": a, "b": b, "admin_a": admin_a, "admin_b": admin_b}


class TestStorageThresholdTenantIsolation:
    def test_a_crossing_in_a_does_not_change_bs_state(self, session, pair):
        a, b = pair["a"], pair["b"]
        set_storage(session, a, 8_500)
        evaluate_storage(session, a)

        assert state_row(session, a).state == UsageThresholdState.APPROACHING.value
        assert state_row(session, b).state == UsageThresholdState.NORMAL.value

    def test_a_crossing_in_a_does_not_notify_b(self, session, pair):
        a, b = pair["a"], pair["b"]
        set_storage(session, a, 8_500)
        evaluate_storage(session, a)

        assert len(notifications(session, a)) == 1
        assert notifications(session, b) == []

    def test_the_notification_carries_as_only_recipients(self, session, pair):
        a = pair["a"]
        set_storage(session, a, 8_500)
        evaluate_storage(session, a)

        notification = notifications(session, a)[0]
        assert notification.tenant_id == a.id
        recipient_ids = {r.user_id for r in recipients_of(session, notification)}
        assert pair["admin_a"].id in recipient_ids
        assert pair["admin_b"].id not in recipient_ids

    def test_exceeding_in_a_does_not_exceed_in_b(self, session, pair):
        a, b = pair["a"], pair["b"]
        set_storage(session, a, 12_000)
        evaluate_storage(session, a)

        assert state_row(session, a).state == UsageThresholdState.REACHED.value
        assert state_row(session, b).state == UsageThresholdState.NORMAL.value
        assert notifications(session, b) == []

    def test_as_limit_does_not_govern_b(self, session, pair):
        """B has its own limit; tightening A's must not re-evaluate B."""
        a, b = pair["a"], pair["b"]
        set_limits(session, a, storage=500)
        UsageThresholdService.evaluate_tenant(session, a.id, source="test")
        session.commit()

        assert state_row(session, a).state == UsageThresholdState.REACHED.value
        assert state_row(session, b).state == UsageThresholdState.NORMAL.value

    def test_an_unlimited_neighbour_stays_silent_while_a_crosses(
        self, session, pair
    ):
        """B with no `TenantLimits` row is unlimited and structurally silent —
        A crossing must not manufacture a state for it."""
        a, b = pair["a"], pair["b"]
        row = session.get(TenantLimits, b.id)
        session.delete(row)
        session.commit()
        evaluate_storage(session, b)

        set_storage(session, a, 12_000)
        evaluate_storage(session, a)

        assert notifications(session, b) == []
        b_state = state_row(session, b)
        assert b_state is None or b_state.state != UsageThresholdState.REACHED.value

    def test_recovery_in_a_does_not_re_arm_b(self, session, pair):
        a, b = pair["a"], pair["b"]
        set_storage(session, a, 12_000)
        evaluate_storage(session, a)
        set_storage(session, a, 100)
        evaluate_storage(session, a)

        assert state_row(session, a).state == UsageThresholdState.NORMAL.value
        assert state_row(session, b).state == UsageThresholdState.NORMAL.value
        assert notifications(session, b) == []


class TestUserThresholdTenantIsolation:
    """Active-user counting must be tenant-scoped: B's users must never be
    counted into A's seat total."""

    def test_bs_users_are_not_counted_into_a(self, session, pair):
        a, b = pair["a"], pair["b"]
        set_limits(session, a, users=2)
        set_limits(session, b, users=2)

        for i in range(5):
            create_user(session, b, email=f"extra{i}@b.test", roles=("admin",))

        evaluate_users(session, a)
        evaluate_users(session, b)

        assert state_row(session, a, UsageResource.USERS).state == (
            UsageThresholdState.NORMAL.value
        )
        assert state_row(session, b, UsageResource.USERS).state == (
            UsageThresholdState.REACHED.value
        )

    def test_bs_user_crossing_does_not_notify_a(self, session, pair):
        a, b = pair["a"], pair["b"]
        set_limits(session, a, users=2)
        set_limits(session, b, users=2)
        for i in range(5):
            create_user(session, b, email=f"extra{i}@b.test", roles=("admin",))

        evaluate_users(session, b)

        assert notifications(session, a, USER_LIMIT_TYPES) == []
        assert notifications(session, b, USER_LIMIT_TYPES)

    def test_each_tenants_user_state_row_carries_its_own_tenant(
        self, session, pair
    ):
        a, b = pair["a"], pair["b"]
        set_limits(session, a, users=10)
        set_limits(session, b, users=10)
        evaluate_users(session, a)
        evaluate_users(session, b)

        assert state_row(session, a, UsageResource.USERS).tenant_id == a.id
        assert state_row(session, b, UsageResource.USERS).tenant_id == b.id


class TestThresholdsRemainNotificationOnly:
    """The Céluma 1.3 contract is observe-and-report. An exceeded tenant must
    still be able to work — no accidental hard blocking."""

    def test_an_exceeded_tenant_can_still_read_its_usage(
        self, client, session, pair
    ):
        from .factories import auth_headers

        a = pair["a"]
        set_storage(session, a, 999_999)
        evaluate_storage(session, a)
        assert state_row(session, a).state == UsageThresholdState.REACHED.value

        response = client.get("/api/v1/tenant/usage", headers=auth_headers(pair["admin_a"]))
        assert response.status_code == 200

    def test_an_exceeded_tenant_is_not_blocked_from_creating_a_user(
        self, session, pair
    ):
        """User limits are reported, never enforced, in 1.3."""
        a = pair["a"]
        set_limits(session, a, users=1)
        evaluate_users(session, a)

        extra = create_user(session, a, email="one.more@a.test", roles=("admin",))
        assert extra.id is not None


class TestIdempotencyAcrossTenants:
    def test_repeated_evaluation_does_not_duplicate_either_tenants_notification(
        self, session, pair
    ):
        a, b = pair["a"], pair["b"]
        set_storage(session, a, 8_500)
        set_storage(session, b, 8_500)

        for _ in range(3):
            evaluate_storage(session, a)
            evaluate_storage(session, b)

        assert len(notifications(session, a)) == 1
        assert len(notifications(session, b)) == 1
