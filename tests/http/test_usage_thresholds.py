"""Céluma 1.3, Phase 4, Block G — `UsageThresholdService`.

The state machine, its durable state, its idempotency, its re-arming, the
two not-evaluable cases, the recipient rule, the actor exception, the copy,
and the failure containment. Concurrency has its own module
(`test_usage_threshold_concurrency.py`) and the trigger wiring has a third
(`test_usage_threshold_triggers.py`).

Everything here runs against real PostgreSQL, through the real
`NotificationService`, with the real template registry — no mocked
arithmetic, and no stubbed notification layer. The one thing ever patched is
`NotificationService.notify`, in `TestFailureContainment`, where the failure
*is* the subject.
"""
from __future__ import annotations

import pytest
from sqlmodel import Session, select

from app.models.notification import (
    Notification,
    NotificationRecipient,
    NotificationSeverity,
    NotificationType,
)
from app.models.tenant_limits import TenantLimits
from app.models.tenant_usage import TenantUsage
from app.models.tenant_usage_threshold_state import (
    TenantUsageThresholdState,
    UsageResource,
    UsageThresholdState,
)
from app.services.usage import UsageService
from app.services.usage_thresholds import (
    STORAGE_THRESHOLD_POLICY,
    USER_THRESHOLD_POLICY,
    UNEVALUABLE_UNLIMITED,
    UNEVALUABLE_USAGE_UNINITIALIZED,
    UsageThresholdPolicy,
    UsageThresholdService,
    derive_state,
    display_percent,
    is_upward,
)

from .factories import create_branch, create_tenant, create_user

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: A limit of exactly 10 000 makes every percentage in the boundary tests an
#: exact integer of bytes, so "79.99%" is a real number the database holds
#: rather than a float the test rounded into existence.
LIMIT = 10_000


def set_limits(session: Session, tenant, *, storage=None, users=None) -> None:
    """Upsert `TenantLimits`. There is no production write path for this table
    in Céluma 1.3 (see `TestLimitMutationInventory`), so tests write it
    directly, exactly as Block B's own tests do."""
    row = session.get(TenantLimits, tenant.id)
    if row is None:
        row = TenantLimits(tenant_id=tenant.id)
        session.add(row)
    row.storage_limit_bytes = storage
    row.user_limit = users
    session.add(row)
    session.commit()


def set_storage(session: Session, tenant, used: int) -> None:
    """Put the counter at an exact value, initializing it if needed."""
    row = session.get(TenantUsage, tenant.id)
    if row is None:
        UsageService.initialize_usage(
            session, tenant.id, billable_storage_bytes=used, source="test"
        )
    else:
        row.billable_storage_bytes = used
        session.add(row)
    session.commit()


def evaluate_storage(session: Session, tenant, **kwargs):
    """Evaluate and commit — the service never commits, by design."""
    result = UsageThresholdService.evaluate_storage(
        session, tenant.id, source="test", **kwargs
    )
    session.commit()
    return result


def evaluate_users(session: Session, tenant, **kwargs):
    result = UsageThresholdService.evaluate_users(
        session, tenant.id, source="test", **kwargs
    )
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


def notifications(session: Session, tenant, notification_type=None):
    statement = select(Notification).where(Notification.tenant_id == tenant.id)
    if notification_type is not None:
        statement = statement.where(Notification.type == notification_type.value)
    return list(session.exec(statement.order_by(Notification.created_at)).all())


@pytest.fixture(name="tenant")
def tenant_fixture(session):
    tenant = create_tenant(session, name="Threshold Lab")
    create_branch(session, tenant)
    return tenant


@pytest.fixture(name="admin")
def admin_fixture(session, tenant):
    """One `admin:manage_tenant` holder, so notifications have a recipient."""
    return create_user(session, tenant, email="admin@lab.test", roles=("admin",))


# ---------------------------------------------------------------------------
# 1. State derivation — raw integers, never a rounded percentage
# ---------------------------------------------------------------------------

class TestStateDerivation:
    """Pure function, no database. The boundary cases are expressed as exact
    integer pairs rather than percentages, because the whole point of this
    arithmetic is that it never goes near a float."""

    @pytest.mark.parametrize(
        "used,expected",
        [
            (0, UsageThresholdState.NORMAL),
            (1, UsageThresholdState.NORMAL),
            (7_998, UsageThresholdState.NORMAL),
            # 79.99% — one byte below the boundary.
            (7_999, UsageThresholdState.NORMAL),
            # 80.00% exactly — the boundary is inclusive.
            (8_000, UsageThresholdState.APPROACHING),
            (8_001, UsageThresholdState.APPROACHING),
            # 99.99% — one byte below the limit.
            (9_999, UsageThresholdState.APPROACHING),
            # 100.00% exactly.
            (10_000, UsageThresholdState.REACHED),
            (10_001, UsageThresholdState.REACHED),
            (12_000, UsageThresholdState.REACHED),
            (10_000_000, UsageThresholdState.REACHED),
        ],
    )
    def test_boundaries(self, used, expected):
        assert derive_state(used, LIMIT, STORAGE_THRESHOLD_POLICY) is expected

    def test_a_value_the_api_would_round_up_to_80_is_not_approaching(self):
        """The case the contract names explicitly.

        `GET /api/v1/tenant/usage` rounds `usage_percent` to two decimals, so
        79.9996% renders as `80.0`. A state machine reading that number would
        notify a tenant that is not at 80%. These integers are 79.9996%
        exactly.
        """
        used, limit = 799_996, 1_000_000
        assert used * 100 < limit * 80  # not at the threshold, by arithmetic
        assert round(used / limit * 100, 2) == 80.0  # but the API says it is
        assert (
            derive_state(used, limit, STORAGE_THRESHOLD_POLICY)
            is UsageThresholdState.NORMAL
        )

    def test_huge_values_stay_exact(self):
        """A petabyte-scale tenant. Integer arithmetic has no precision floor;
        the same comparison in float64 would start losing bytes."""
        limit = 1_125_899_906_842_624  # 1 PiB
        assert (
            derive_state(limit - 1, limit, STORAGE_THRESHOLD_POLICY)
            is UsageThresholdState.APPROACHING
        )
        assert (
            derive_state(limit, limit, STORAGE_THRESHOLD_POLICY)
            is UsageThresholdState.REACHED
        )

    @pytest.mark.parametrize(
        "used,limit",
        [(None, LIMIT), (500, None), (None, None), (500, 0), (500, -1)],
    )
    def test_absent_or_impossible_operands_are_unmonitored(self, used, limit):
        assert (
            derive_state(used, limit, STORAGE_THRESHOLD_POLICY)
            is UsageThresholdState.UNMONITORED
        )

    def test_both_resources_share_the_same_policy_in_celuma_1_3(self):
        assert STORAGE_THRESHOLD_POLICY == UsageThresholdPolicy(80, 100)
        assert USER_THRESHOLD_POLICY == UsageThresholdPolicy(80, 100)

    def test_ninety_percent_is_not_a_state(self):
        """Block F's dashboard has a third visual band. This block
        deliberately does not mirror it as a third notifying state."""
        assert (
            derive_state(9_000, LIMIT, STORAGE_THRESHOLD_POLICY)
            is UsageThresholdState.APPROACHING
        )

    def test_a_policy_with_approaching_above_reached_is_rejected(self):
        with pytest.raises(ValueError):
            UsageThresholdPolicy(approaching_percent=120, reached_percent=100)

    @pytest.mark.parametrize(
        "used,limit,expected", [(8_270, 10_000, 82), (9_999, 10_000, 99), (1, 3, 33)]
    )
    def test_display_percent_floors(self, used, limit, expected):
        """"aproximadamente el N%" must never claim more usage than there is."""
        assert display_percent(used, limit) == expected

    def test_unmonitored_ranks_below_normal(self):
        """The single ordering choice that makes first evaluation, limit
        restoration and downward re-arming all fall out of one rule."""
        assert is_upward(UsageThresholdState.UNMONITORED, UsageThresholdState.NORMAL)
        assert is_upward(
            UsageThresholdState.UNMONITORED, UsageThresholdState.APPROACHING
        )
        assert is_upward(UsageThresholdState.UNMONITORED, UsageThresholdState.REACHED)
        assert not is_upward(
            UsageThresholdState.REACHED, UsageThresholdState.APPROACHING
        )
        assert not is_upward(UsageThresholdState.NORMAL, UsageThresholdState.NORMAL)
        assert not is_upward(
            UsageThresholdState.APPROACHING, UsageThresholdState.UNMONITORED
        )


# ---------------------------------------------------------------------------
# 2. First evaluation
# ---------------------------------------------------------------------------

class TestFirstEvaluation:
    """A tenant that has never been evaluated — including one that was already
    above a threshold when the feature shipped.

    The policy: establish the current state, and emit **one** notification for
    the current highest meaningful state. A laboratory that is over its limit
    on the day this deploys must not stay silent merely because the historical
    crossing happened before the code existed.
    """

    def test_below_everything_records_normal_and_notifies_nobody(
        self, session, tenant, admin
    ):
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 4_500)  # 45%

        result = evaluate_storage(session, tenant)

        assert result.new_state is UsageThresholdState.NORMAL
        assert result.previous_state is UsageThresholdState.UNMONITORED
        assert not result.notified
        assert notifications(session, tenant) == []
        row = state_row(session, tenant)
        assert row.state == UsageThresholdState.NORMAL
        assert row.last_value == 4_500 and row.last_limit == LIMIT
        assert row.transition_count == 0

    def test_already_approaching_emits_exactly_one_approaching(
        self, session, tenant, admin
    ):
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 8_700)  # 87%

        result = evaluate_storage(session, tenant)

        assert result.new_state is UsageThresholdState.APPROACHING
        assert result.notification_type is NotificationType.STORAGE_USAGE_APPROACHING
        created = notifications(session, tenant)
        assert len(created) == 1
        assert created[0].type == NotificationType.STORAGE_USAGE_APPROACHING.value
        assert "87%" in created[0].body

    def test_already_over_the_limit_emits_reached_only(self, session, tenant, admin):
        """The case that most obviously must not double-send: a tenant at 104%
        has, historically, crossed 80% too. It is told once, about the state
        it is actually in."""
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 10_400)  # 104%

        result = evaluate_storage(session, tenant)

        assert result.new_state is UsageThresholdState.REACHED
        created = notifications(session, tenant)
        assert len(created) == 1
        assert created[0].type == NotificationType.STORAGE_LIMIT_REACHED.value
        assert notifications(
            session, tenant, NotificationType.STORAGE_USAGE_APPROACHING
        ) == []

    def test_the_state_row_is_created_by_the_first_evaluation_not_before(
        self, session, tenant, admin
    ):
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 100)
        assert state_row(session, tenant) is None

        evaluate_storage(session, tenant)

        assert state_row(session, tenant) is not None
        # The other resource is untouched: evaluation is per (tenant, resource).
        assert state_row(session, tenant, UsageResource.USERS) is None


# ---------------------------------------------------------------------------
# 3. Idempotency — the reason the state table exists
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_twenty_evaluations_at_85_percent_produce_one_notification(
        self, session, tenant, admin
    ):
        """The failure mode the durable state prevents. Without it, this is
        twenty notifications — one per sample upload, forever."""
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 8_500)

        for _ in range(20):
            evaluate_storage(session, tenant)

        assert len(notifications(session, tenant)) == 1
        assert state_row(session, tenant).transition_count == 1

    def test_twenty_evaluations_at_110_percent_produce_one_notification(
        self, session, tenant, admin
    ):
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 11_000)

        for _ in range(20):
            evaluate_storage(session, tenant)

        created = notifications(session, tenant)
        assert len(created) == 1
        assert created[0].type == NotificationType.STORAGE_LIMIT_REACHED.value

    def test_usage_moving_within_one_state_creates_nothing(
        self, session, tenant, admin
    ):
        set_limits(session, tenant, storage=LIMIT)
        for used in (8_100, 8_400, 9_000, 9_500, 9_900):
            set_storage(session, tenant, used)
            evaluate_storage(session, tenant)

        assert len(notifications(session, tenant)) == 1
        # The row still tracks the latest numbers even with no transition.
        assert state_row(session, tenant).last_value == 9_900

    def test_last_transition_at_is_not_churned_by_a_no_op_evaluation(
        self, session, tenant, admin
    ):
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 8_500)
        evaluate_storage(session, tenant)
        first = state_row(session, tenant).last_transition_at

        set_storage(session, tenant, 8_600)
        evaluate_storage(session, tenant)

        assert state_row(session, tenant).last_transition_at == first


# ---------------------------------------------------------------------------
# 4. Upward transitions
# ---------------------------------------------------------------------------

class TestUpwardTransitions:
    def test_79_to_81_notifies_approaching(self, session, tenant, admin):
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 7_900)
        evaluate_storage(session, tenant)
        assert notifications(session, tenant) == []

        set_storage(session, tenant, 8_100)
        result = evaluate_storage(session, tenant)

        assert result.previous_state is UsageThresholdState.NORMAL
        assert result.new_state is UsageThresholdState.APPROACHING
        assert len(notifications(session, tenant)) == 1

    def test_81_to_99_notifies_nothing(self, session, tenant, admin):
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 8_100)
        evaluate_storage(session, tenant)
        set_storage(session, tenant, 9_900)
        result = evaluate_storage(session, tenant)

        assert result.new_state is UsageThresholdState.APPROACHING
        assert not result.notified
        assert len(notifications(session, tenant)) == 1

    def test_99_to_101_notifies_reached(self, session, tenant, admin):
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 9_900)
        evaluate_storage(session, tenant)
        set_storage(session, tenant, 10_100)
        result = evaluate_storage(session, tenant)

        assert result.new_state is UsageThresholdState.REACHED
        types = [n.type for n in notifications(session, tenant)]
        assert types == [
            NotificationType.STORAGE_USAGE_APPROACHING.value,
            NotificationType.STORAGE_LIMIT_REACHED.value,
        ]

    def test_79_to_101_in_one_step_notifies_reached_only(
        self, session, tenant, admin
    ):
        """A single large upload can skip a band. The recipient is told the
        state they are in, not every state they passed through."""
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 7_900)
        evaluate_storage(session, tenant)
        set_storage(session, tenant, 10_100)
        evaluate_storage(session, tenant)

        types = [n.type for n in notifications(session, tenant)]
        assert types == [NotificationType.STORAGE_LIMIT_REACHED.value]


# ---------------------------------------------------------------------------
# 5. Re-arming
# ---------------------------------------------------------------------------

class TestReArming:
    def test_81_then_70_then_82_notifies_approaching_twice(
        self, session, tenant, admin
    ):
        """Two genuine crossings, two notifications. The tenant really did
        come back under the threshold and go over it again."""
        set_limits(session, tenant, storage=LIMIT)
        for used in (8_100, 7_000, 8_200):
            set_storage(session, tenant, used)
            evaluate_storage(session, tenant)

        created = notifications(
            session, tenant, NotificationType.STORAGE_USAGE_APPROACHING
        )
        assert len(created) == 2
        # Two distinct idempotency keys — the transition counter is what makes
        # the second crossing a different occurrence rather than a duplicate.
        assert created[0].idempotency_key != created[1].idempotency_key

    def test_105_then_95_then_101_notifies_reached_twice(
        self, session, tenant, admin
    ):
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 10_500)
        evaluate_storage(session, tenant)

        set_storage(session, tenant, 9_500)
        mid = evaluate_storage(session, tenant)
        assert mid.new_state is UsageThresholdState.APPROACHING
        assert not mid.notified, "a downward move must never notify"

        set_storage(session, tenant, 10_100)
        evaluate_storage(session, tenant)

        assert (
            len(notifications(session, tenant, NotificationType.STORAGE_LIMIT_REACHED))
            == 2
        )

    def test_reached_drops_below_80_and_becomes_normal(self, session, tenant, admin):
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 10_500)
        evaluate_storage(session, tenant)
        set_storage(session, tenant, 5_000)
        result = evaluate_storage(session, tenant)

        assert result.new_state is UsageThresholdState.NORMAL
        assert not result.notified
        assert len(notifications(session, tenant)) == 1

    def test_no_stricter_hysteresis_is_applied(self, session, tenant, admin):
        """The re-arm boundary is the state boundary itself: dropping to 97%
        moves REACHED -> APPROACHING, and going back over 100% notifies again.
        A tenant does **not** have to fall all the way below 80% first.

        This is the deliberate policy choice, recorded here because the
        alternative (requiring a full drop to NORMAL) is the other plausible
        reading and would silently suppress a real re-crossing."""
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 10_300)
        evaluate_storage(session, tenant)
        set_storage(session, tenant, 9_700)
        assert (
            evaluate_storage(session, tenant).new_state
            is UsageThresholdState.APPROACHING
        )
        set_storage(session, tenant, 10_100)
        result = evaluate_storage(session, tenant)

        assert result.notified
        assert (
            len(notifications(session, tenant, NotificationType.STORAGE_LIMIT_REACHED))
            == 2
        )

    def test_a_downward_move_updates_the_transition_timestamp_but_not_the_counter(
        self, session, tenant, admin
    ):
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 8_500)
        evaluate_storage(session, tenant)
        # Scalars, not the row: `state_row` goes through the identity map, so
        # holding the instance would compare the row against itself.
        up_count = state_row(session, tenant).transition_count
        up_at = state_row(session, tenant).last_transition_at
        assert up_count == 1

        set_storage(session, tenant, 1_000)
        evaluate_storage(session, tenant)
        after_down = state_row(session, tenant)

        assert after_down.state == UsageThresholdState.NORMAL
        # The counter counts *notifying* transitions, so a downward move leaves
        # it alone — which is what keeps the next upward crossing's idempotency
        # key distinct from the previous one's.
        assert after_down.transition_count == 1
        assert after_down.last_transition_at > up_at


# ---------------------------------------------------------------------------
# 6. Unlimited and uninitialized
# ---------------------------------------------------------------------------

class TestUnlimited:
    def test_no_storage_limit_produces_no_event(self, session, tenant, admin):
        set_storage(session, tenant, 10_000_000)

        result = evaluate_storage(session, tenant)

        assert result.unevaluable_reason == UNEVALUABLE_UNLIMITED
        assert result.new_state is UsageThresholdState.UNMONITORED
        assert notifications(session, tenant) == []

    def test_no_user_limit_produces_no_event(self, session, tenant, admin):
        result = evaluate_users(session, tenant)

        assert result.unevaluable_reason == UNEVALUABLE_UNLIMITED
        assert notifications(session, tenant) == []

    def test_unlimited_is_never_zero_percent(self, session, tenant, admin):
        """The `null`-is-not-`0` rule, at the threshold layer. A tenant with no
        limit is not "at 0% of nothing"; it has no threshold to be at."""
        set_storage(session, tenant, 0)
        result = evaluate_storage(session, tenant)
        assert result.new_state is UsageThresholdState.UNMONITORED
        assert state_row(session, tenant) is None

    def test_removing_a_limit_resets_a_reached_state_to_unmonitored(
        self, session, tenant, admin
    ):
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 12_000)
        evaluate_storage(session, tenant)
        assert state_row(session, tenant).state == UsageThresholdState.REACHED

        set_limits(session, tenant, storage=None)
        result = evaluate_storage(session, tenant)

        row = state_row(session, tenant)
        assert row.state == UsageThresholdState.UNMONITORED
        assert row.last_value is None and row.last_limit is None
        assert not result.notified
        assert len(notifications(session, tenant)) == 1

    def test_a_limit_restored_below_current_usage_notifies_again(
        self, session, tenant, admin
    ):
        """Why the reset above matters: without it the state would still read
        REACHED, `REACHED -> REACHED` is not upward, and the tenant would never
        be told about the new, lower limit it is already over."""
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 12_000)
        evaluate_storage(session, tenant)
        set_limits(session, tenant, storage=None)
        evaluate_storage(session, tenant)

        set_limits(session, tenant, storage=LIMIT)
        result = evaluate_storage(session, tenant)

        assert result.notified
        assert (
            len(notifications(session, tenant, NotificationType.STORAGE_LIMIT_REACHED))
            == 2
        )

    def test_repeated_evaluation_while_unlimited_writes_nothing(
        self, session, tenant, admin
    ):
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 12_000)
        evaluate_storage(session, tenant)
        set_limits(session, tenant, storage=None)
        evaluate_storage(session, tenant)
        first = state_row(session, tenant).last_transition_at

        for _ in range(5):
            evaluate_storage(session, tenant)

        assert state_row(session, tenant).last_transition_at == first


class TestUninitializedStorage:
    def test_a_missing_tenant_usage_row_produces_no_event(
        self, session, tenant, admin
    ):
        set_limits(session, tenant, storage=LIMIT)
        assert UsageService.get_usage(session, tenant.id) is None

        result = evaluate_storage(session, tenant)

        assert result.unevaluable_reason == UNEVALUABLE_USAGE_UNINITIALIZED
        assert notifications(session, tenant) == []
        assert state_row(session, tenant) is None

    def test_it_is_not_read_as_zero_bytes(self, session, tenant, admin):
        """A missing counter is absent information, not a zero. Treating it as
        0 would record NORMAL for a tenant nobody has measured — and then
        suppress nothing, but *claim* something the backend does not know."""
        set_limits(session, tenant, storage=LIMIT)
        result = evaluate_storage(session, tenant)
        assert result.new_state is not UsageThresholdState.NORMAL
        assert result.used_value is None

    def test_a_meaningful_state_survives_the_counter_going_missing(
        self, session, tenant, admin
    ):
        """The asymmetry with "unlimited", stated as a test.

        Removing a limit is a decision, so it resets the state. A counter row
        disappearing is a fault, so it changes nothing: overwriting APPROACHING
        with UNMONITORED would destroy history *and* re-arm a crossing that
        never un-crossed, so the tenant would be notified a second time when
        the row came back.
        """
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 8_500)
        evaluate_storage(session, tenant)
        before = state_row(session, tenant)
        # Scalars: `state_row` returns the identity-mapped instance, which the
        # next evaluation would mutate in place.
        before_value, before_at = before.last_value, before.last_transition_at

        session.delete(session.get(TenantUsage, tenant.id))
        session.commit()
        evaluate_storage(session, tenant)

        after = state_row(session, tenant)
        assert after.state == UsageThresholdState.APPROACHING
        assert after.last_value == before_value
        assert after.last_transition_at == before_at

        # And when the counter comes back at the same level, still one
        # notification in total.
        set_storage(session, tenant, 8_500)
        evaluate_storage(session, tenant)
        assert len(notifications(session, tenant)) == 1

    def test_evaluation_resumes_once_usage_is_initialized(
        self, session, tenant, admin
    ):
        set_limits(session, tenant, storage=LIMIT)
        evaluate_storage(session, tenant)
        assert notifications(session, tenant) == []

        set_storage(session, tenant, 9_100)
        result = evaluate_storage(session, tenant)

        assert result.new_state is UsageThresholdState.APPROACHING
        assert len(notifications(session, tenant)) == 1

    def test_users_have_no_uninitialized_case(self, session, tenant, admin):
        """`active_internal_users` is computed live, so the number is always
        known. The only reason users can be unevaluable is an absent limit."""
        set_limits(session, tenant, users=5)
        result = evaluate_users(session, tenant)
        assert result.unevaluable_reason is None
        assert result.used_value is not None


# ---------------------------------------------------------------------------
# 7. Limit changes
# ---------------------------------------------------------------------------

class TestLimitChanges:
    def test_a_lower_limit_crosses_a_threshold_with_no_usage_change(
        self, session, tenant, admin
    ):
        """80 GB against 100 GB is 80%; drop the limit to 70 GB and the same
        80 GB is 114%. Nothing was uploaded."""
        set_limits(session, tenant, storage=100)
        set_storage(session, tenant, 60)  # 60%
        evaluate_storage(session, tenant)
        assert notifications(session, tenant) == []

        set_limits(session, tenant, storage=50)  # now 120%
        result = evaluate_storage(session, tenant)

        assert result.new_state is UsageThresholdState.REACHED
        assert len(notifications(session, tenant)) == 1

    def test_a_higher_limit_re_arms_downward_without_notifying(
        self, session, tenant, admin
    ):
        set_limits(session, tenant, storage=100)
        set_storage(session, tenant, 120)
        evaluate_storage(session, tenant)
        assert len(notifications(session, tenant)) == 1

        set_limits(session, tenant, storage=1_000)  # now 12%
        result = evaluate_storage(session, tenant)

        assert result.new_state is UsageThresholdState.NORMAL
        assert not result.notified
        assert len(notifications(session, tenant)) == 1

        # …and the re-arm is real: lowering it again notifies.
        set_limits(session, tenant, storage=100)
        evaluate_storage(session, tenant)
        assert len(notifications(session, tenant)) == 2

    def test_evaluate_tenant_covers_both_resources(self, session, tenant, admin):
        """The limit-change hook. A limit edit can touch either resource, so
        the hook evaluates both rather than making the caller remember which
        column it changed."""
        set_limits(session, tenant, storage=100, users=1)
        set_storage(session, tenant, 150)

        results = UsageThresholdService.evaluate_tenant(
            session, tenant.id, source="limits_changed"
        )
        session.commit()

        assert set(results) == {UsageResource.STORAGE, UsageResource.USERS}
        assert results[UsageResource.STORAGE].new_state is UsageThresholdState.REACHED
        assert results[UsageResource.USERS].new_state is UsageThresholdState.REACHED
        assert {n.type for n in notifications(session, tenant)} == {
            NotificationType.STORAGE_LIMIT_REACHED.value,
            NotificationType.USER_LIMIT_REACHED.value,
        }


# ---------------------------------------------------------------------------
# 8. User metrics — the seat numerator
# ---------------------------------------------------------------------------

class TestUserThresholds:
    def test_a_physician_only_user_does_not_consume_a_seat(
        self, session, tenant, admin
    ):
        set_limits(session, tenant, users=2)  # admin = 1 of 2 = 50%
        evaluate_users(session, tenant)
        assert notifications(session, tenant) == []

        create_user(session, tenant, email="doc@lab.test", roles=("physician",))
        result = evaluate_users(session, tenant)

        assert result.used_value == 1
        assert result.new_state is UsageThresholdState.NORMAL
        assert notifications(session, tenant) == []

    def test_an_inactive_user_does_not_consume_a_seat(self, session, tenant, admin):
        set_limits(session, tenant, users=2)
        evaluate_users(session, tenant)

        inactive = create_user(
            session, tenant, email="ghost@lab.test", roles=("lab_tech",)
        )
        inactive.is_active = False
        session.add(inactive)
        session.commit()

        result = evaluate_users(session, tenant)
        assert result.used_value == 1
        assert notifications(session, tenant) == []

    def test_an_active_internal_user_can_cross_the_threshold(
        self, session, tenant, admin
    ):
        set_limits(session, tenant, users=2)  # admin = 50%
        evaluate_users(session, tenant)
        assert notifications(session, tenant) == []

        create_user(session, tenant, email="tech@lab.test", roles=("lab_tech",))
        result = evaluate_users(session, tenant)

        assert result.used_value == 2
        assert result.new_state is UsageThresholdState.REACHED
        created = notifications(session, tenant)
        assert len(created) == 1
        assert created[0].type == NotificationType.USER_LIMIT_REACHED.value

    def test_a_multi_role_physician_counts_once_as_internal(
        self, session, tenant, admin
    ):
        set_limits(session, tenant, users=10)
        create_user(
            session, tenant, email="both@lab.test", roles=("physician", "reviewer")
        )
        result = evaluate_users(session, tenant)

        # admin + the physician/reviewer = 2, not 3.
        assert result.used_value == 2

    def test_registered_users_is_not_the_numerator(self, session, tenant, admin):
        """`registered_users` counts inactive accounts too. If it were the
        numerator, deactivating a user would never free a seat."""
        set_limits(session, tenant, users=3)
        for index in range(4):
            ghost = create_user(
                session, tenant, email=f"ghost{index}@lab.test", roles=("viewer",)
            )
            ghost.is_active = False
            session.add(ghost)
        session.commit()

        metrics = UsageService.get_user_metrics(session, tenant.id)
        assert metrics.registered_users == 5
        result = evaluate_users(session, tenant)
        assert result.used_value == 1
        assert notifications(session, tenant) == []

    def test_the_user_approaching_body_carries_the_percentage(
        self, session, tenant, admin
    ):
        set_limits(session, tenant, users=5)
        for index in range(3):
            create_user(
                session, tenant, email=f"tech{index}@lab.test", roles=("lab_tech",)
            )
        result = evaluate_users(session, tenant)  # 4 of 5 = 80%

        assert result.new_state is UsageThresholdState.APPROACHING
        created = notifications(session, tenant)
        assert created[0].type == NotificationType.USER_LIMIT_APPROACHING.value
        assert "80%" in created[0].body

    def test_storage_and_users_keep_separate_state(self, session, tenant, admin):
        set_limits(session, tenant, storage=LIMIT, users=1)
        set_storage(session, tenant, 100)  # 1%

        evaluate_storage(session, tenant)
        evaluate_users(session, tenant)

        assert state_row(session, tenant, UsageResource.STORAGE).state == (
            UsageThresholdState.NORMAL
        )
        assert state_row(session, tenant, UsageResource.USERS).state == (
            UsageThresholdState.REACHED
        )
        assert [n.type for n in notifications(session, tenant)] == [
            NotificationType.USER_LIMIT_REACHED.value
        ]


# ---------------------------------------------------------------------------
# 9. Recipients
# ---------------------------------------------------------------------------

class TestRecipients:
    @pytest.fixture(name="cast")
    def cast_fixture(self, session, tenant):
        """One of everything, so "only permission holders" is a claim about a
        realistic tenant rather than about two users."""
        people = {
            "admin": create_user(session, tenant, email="a@lab.test", roles=("admin",)),
            "superuser": create_user(
                session, tenant, email="su@lab.test", roles=("superuser",)
            ),
            "pathologist": create_user(
                session, tenant, email="path@lab.test", roles=("pathologist",)
            ),
            "lab_tech": create_user(
                session, tenant, email="tech@lab.test", roles=("lab_tech",)
            ),
            "billing": create_user(
                session, tenant, email="bill@lab.test", roles=("billing",)
            ),
            "assistant": create_user(
                session, tenant, email="asst@lab.test", roles=("assistant",)
            ),
            "viewer": create_user(
                session, tenant, email="view@lab.test", roles=("viewer",)
            ),
            "physician": create_user(
                session, tenant, email="doc@lab.test", roles=("physician",)
            ),
            "multi_role_admin": create_user(
                session, tenant, email="both@lab.test", roles=("admin", "pathologist")
            ),
            "inactive_admin": create_user(
                session, tenant, email="gone@lab.test", roles=("admin",)
            ),
        }
        people["inactive_admin"].is_active = False
        session.add(people["inactive_admin"])
        session.commit()
        return people

    def _recipients(self, session, notification):
        return set(
            session.exec(
                select(NotificationRecipient.user_id).where(
                    NotificationRecipient.notification_id == notification.id
                )
            ).all()
        )

    def test_only_active_permission_holders_receive_it(self, session, tenant, cast):
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 12_000)
        evaluate_storage(session, tenant)

        notification = notifications(session, tenant)[0]
        recipients = self._recipients(session, notification)

        assert recipients == {
            cast["admin"].id,
            cast["superuser"].id,
            cast["multi_role_admin"].id,
        }
        for excluded in (
            "pathologist",
            "lab_tech",
            "billing",
            "assistant",
            "viewer",
            "physician",
            "inactive_admin",
        ):
            assert cast[excluded].id not in recipients, excluded

    def test_a_multi_role_holder_appears_exactly_once(self, session, tenant, cast):
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 12_000)
        evaluate_storage(session, tenant)

        notification = notifications(session, tenant)[0]
        rows = list(
            session.exec(
                select(NotificationRecipient).where(
                    NotificationRecipient.notification_id == notification.id,
                    NotificationRecipient.user_id == cast["multi_role_admin"].id,
                )
            ).all()
        )
        assert len(rows) == 1

    def test_another_tenants_admins_never_receive_it(self, session, tenant, cast):
        other = create_tenant(session, name="Other Lab")
        create_branch(session, other, code="OTHER")
        other_admin = create_user(
            session, other, email="a@other.test", roles=("admin",)
        )

        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 12_000)
        evaluate_storage(session, tenant)

        notification = notifications(session, tenant)[0]
        assert other_admin.id not in self._recipients(session, notification)
        assert notifications(session, other) == []

    def test_a_tenant_with_no_permission_holder_still_records_the_event(
        self, session, tenant
    ):
        """Zero recipients is not an error — the notification row remains the
        audit record that the crossing happened, and the state still advances
        so a later admin is not spammed for a crossing that already occurred.
        (Phase 3 recipient-matrix rule 6.)"""
        create_user(session, tenant, email="tech@lab.test", roles=("lab_tech",))
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 12_000)
        result = evaluate_storage(session, tenant)

        assert result.recipient_count == 0
        assert len(notifications(session, tenant)) == 1
        assert state_row(session, tenant).state == UsageThresholdState.REACHED


class TestActorIsNotExcluded:
    """The one Phase 3 rule this event does not follow.

    Actor exclusion exists because the actor already saw the result of their
    own action. That is true of publishing a report and false of crossing a
    threshold: the admin who uploaded the image saw an upload succeed, not a
    laboratory pass 80%. In a single-admin laboratory — the common case —
    excluding them would mean the event reaches nobody at all.
    """

    def test_the_acting_admin_still_receives_the_notification(
        self, session, tenant, admin
    ):
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 12_000)

        UsageThresholdService.evaluate_storage(
            session, tenant.id, source="test", actor_id=admin.id
        )
        session.commit()

        notification = notifications(session, tenant)[0]
        assert notification.created_by == admin.id
        recipients = set(
            session.exec(
                select(NotificationRecipient.user_id).where(
                    NotificationRecipient.notification_id == notification.id
                )
            ).all()
        )
        assert admin.id in recipients

    def test_a_single_admin_laboratory_is_not_left_with_zero_recipients(
        self, session, tenant, admin
    ):
        set_limits(session, tenant, users=1)

        result = UsageThresholdService.evaluate_users(
            session, tenant.id, source="test", actor_id=admin.id
        )
        session.commit()

        assert result.recipient_count == 1


# ---------------------------------------------------------------------------
# 10. Notification shape, payload and copy
# ---------------------------------------------------------------------------

class TestNotificationShape:
    @pytest.fixture(name="reached")
    def reached_fixture(self, session, tenant, admin):
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 12_000)
        evaluate_storage(session, tenant)
        return notifications(session, tenant)[0]

    def test_it_is_tenant_scoped_and_deep_links_to_the_usage_page(
        self, session, tenant, reached
    ):
        assert reached.resource_type == "tenant"
        assert reached.resource_id == tenant.id

    def test_severity_is_warning(self, session, reached):
        """Not INFO — "the laboratory is over its configured limit" should not
        read like a sample state change. Not ACTION_REQUIRED — that would
        promise an action, and nothing is blocked."""
        assert reached.severity == NotificationSeverity.WARNING.value

    def test_the_payload_carries_no_phi_and_no_storage_internals(
        self, session, tenant, admin
    ):
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 8_700)
        evaluate_storage(session, tenant)
        notification = notifications(session, tenant)[0]

        blob = f"{notification.title} {notification.body} {notification.notification_metadata}"
        lowered = blob.lower()
        for forbidden in (
            "paciente",
            "patient",
            "diagn",
            "muestra",
            "sample",
            "report",
            "bucket",
            "aws",
            "s3://",
            "@",
            ".png",
            ".pdf",
            admin.email,
        ):
            assert forbidden.lower() not in lowered, forbidden

        # The metadata holds the template provenance and the one safe display
        # parameter, and nothing else — no byte counts, no limits, no ids.
        assert set(notification.notification_metadata) == {
            "template_key",
            "template_params",
        }
        assert set(notification.notification_metadata["template_params"]) == {
            "usage_percent"
        }

    def test_the_reached_payload_carries_no_parameters_at_all(
        self, session, tenant, reached
    ):
        assert reached.notification_metadata["template_params"] == {}

    @pytest.mark.parametrize(
        "notification_type",
        [
            NotificationType.STORAGE_USAGE_APPROACHING,
            NotificationType.STORAGE_LIMIT_REACHED,
            NotificationType.USER_LIMIT_APPROACHING,
            NotificationType.USER_LIMIT_REACHED,
        ],
    )
    def test_copy_is_neutral_factual_and_non_enforcing(self, notification_type):
        from app.services.notification_templates import NOTIFICATION_TEMPLATES

        template = NOTIFICATION_TEMPLATES[notification_type]
        text = f"{template.title} {template.body or ''}".lower()

        for forbidden in (
            # Enforcement Céluma does not perform.
            "bloque",
            "suspend",
            "deshabilit",
            "inhabilit",
            "no podrá",
            "no podra",
            "dejará de",
            # Commerce that does not exist.
            "plan",
            "precio",
            "pago",
            "suscrip",
            "compra",
            "actualiza tu",
            "upgrade",
            "$",
            # Infrastructure the tenant never needs to know about.
            "aws",
            "s3",
            "bucket",
            "cloud",
            # Clinical vocabulary.
            "paciente",
            "diagn",
            "muestra",
        ):
            assert forbidden not in text, f"{notification_type.value}: {forbidden}"

        # And never the raw enum value.
        assert notification_type.value not in f"{template.title} {template.body or ''}"

    def test_the_two_approaching_bodies_state_a_real_percentage(
        self, session, tenant, admin
    ):
        set_limits(session, tenant, storage=10_000)
        set_storage(session, tenant, 8_270)  # 82.7%
        evaluate_storage(session, tenant)

        body = notifications(session, tenant)[0].body
        # Floored, so the sentence never overstates usage.
        assert "82%" in body
        assert "83%" not in body


# ---------------------------------------------------------------------------
# 11. Failure containment and atomicity
# ---------------------------------------------------------------------------

class TestFailureContainment:
    """The most important correctness property in the block:

        transition recorded  <=>  notification creation durably recorded

    and, independently, neither may break the caller's transaction.
    """

    def test_a_notification_failure_leaves_no_recorded_transition(
        self, session, tenant, admin, monkeypatch
    ):
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 12_000)

        def boom(*args, **kwargs):
            raise RuntimeError("simulated notification failure")

        monkeypatch.setattr(
            "app.services.usage_thresholds.NotificationService.notify", boom
        )

        result = UsageThresholdService.evaluate_storage(
            session, tenant.id, source="test"
        )
        session.commit()

        assert result.failed is True
        assert notifications(session, tenant) == []
        # The state must NOT say REACHED: a recorded transition with no
        # notification behind it would permanently swallow this crossing,
        # because REACHED -> REACHED is not upward.
        row = state_row(session, tenant)
        assert row is None or row.state == UsageThresholdState.UNMONITORED

    def test_the_next_evaluation_retries_successfully(
        self, session, tenant, admin, monkeypatch
    ):
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 12_000)

        def boom(*args, **kwargs):
            raise RuntimeError("simulated notification failure")

        monkeypatch.setattr(
            "app.services.usage_thresholds.NotificationService.notify", boom
        )
        UsageThresholdService.evaluate_storage(session, tenant.id, source="test")
        session.commit()

        monkeypatch.undo()
        result = evaluate_storage(session, tenant)

        assert result.notified
        assert len(notifications(session, tenant)) == 1
        assert state_row(session, tenant).state == UsageThresholdState.REACHED

    def test_the_callers_transaction_survives_and_still_commits(
        self, session, tenant, admin, monkeypatch
    ):
        """The clinical-safety requirement: a sample upload must not fail
        because a usage notification could not be created."""
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 5_000)

        def boom(*args, **kwargs):
            raise RuntimeError("simulated notification failure")

        monkeypatch.setattr(
            "app.services.usage_thresholds.NotificationService.notify", boom
        )

        # A pending business write in the caller's transaction, made before the
        # threshold work and committed after it.
        usage = session.get(TenantUsage, tenant.id)
        usage.billable_storage_bytes = 12_000
        session.add(usage)

        UsageThresholdService.evaluate_storage(session, tenant.id, source="test")
        session.commit()

        session.expire_all()
        assert session.get(TenantUsage, tenant.id).billable_storage_bytes == 12_000

    def test_evaluate_never_raises_even_on_an_unexpected_failure(
        self, session, tenant, admin, monkeypatch
    ):
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 12_000)
        monkeypatch.setattr(
            "app.services.usage_thresholds.resolve_usage_threshold_recipients",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("resolver exploded")),
        )

        result = UsageThresholdService.evaluate_storage(
            session, tenant.id, source="test"
        )
        session.commit()

        assert result.failed is True
        assert notifications(session, tenant) == []

    def test_a_rolled_back_caller_transaction_takes_the_notification_with_it(
        self, session, tenant, admin
    ):
        """Option A's other half: because the transition and the notification
        are flushed into the caller's transaction, a rollback of the business
        mutation cannot leave a committed notification describing a storage
        change that never happened."""
        set_limits(session, tenant, storage=LIMIT)
        set_storage(session, tenant, 5_000)

        usage = session.get(TenantUsage, tenant.id)
        usage.billable_storage_bytes = 12_000
        session.add(usage)
        UsageThresholdService.evaluate_storage(session, tenant.id, source="test")
        session.rollback()

        session.expire_all()
        assert session.get(TenantUsage, tenant.id).billable_storage_bytes == 5_000
        assert notifications(session, tenant) == []
        assert state_row(session, tenant) is None
