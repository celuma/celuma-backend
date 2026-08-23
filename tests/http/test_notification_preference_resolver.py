"""Effective-preference resolver tests (Céluma 1.3, Phase 3, Block D, D14).

Service-level rather than HTTP: the resolver is what
`NotificationService.notify()` consults per recipient, and its contract is
about resolution and query shape, not about a response body. Driving it
through the API would test the router as well and would not be able to count
queries at all.
"""
import uuid

import pytest
from sqlalchemy import event
from sqlmodel import select

from app.models.notification import NotificationPreference, NotificationType
from app.services.notification_preferences import (
    NOTIFICATION_TYPE_ORDER,
    resolve_all_effective_preferences,
    resolve_effective_notification_preferences,
)

from tests.http.factories import create_branch, create_tenant, create_user

SUPPORTED = NotificationType.REPORT_PUBLISHED
UNSUPPORTED = NotificationType.SAMPLE_STATUS_CHANGED


@pytest.fixture(name="world")
def world_fixture(session):
    tenant = create_tenant(session, name="Tenant A")
    create_branch(session, tenant)
    user = create_user(session, tenant, email="user@tenant-a.test")
    peer = create_user(session, tenant, email="peer@tenant-a.test")
    third = create_user(session, tenant, email="third@tenant-a.test")

    other_tenant = create_tenant(session, name="Tenant B")
    create_branch(session, other_tenant)
    stranger = create_user(session, other_tenant, email="stranger@tenant-b.test")

    return {
        "tenant": tenant,
        "user": user,
        "peer": peer,
        "third": third,
        "other_tenant": other_tenant,
        "stranger": stranger,
    }


def override(session, world, user, notification_type, *, email=False, in_app=True):
    row = NotificationPreference(
        tenant_id=user.tenant_id,
        user_id=user.id,
        notification_type=notification_type.value,
        email_enabled=email,
        in_app_enabled=in_app,
    )
    session.add(row)
    session.commit()
    return row


def resolve(session, world, users, notification_type=SUPPORTED, tenant=None):
    return resolve_effective_notification_preferences(
        session,
        tenant_id=(tenant or world["tenant"]).id,
        user_ids=[u.id for u in users],
        notification_type=notification_type,
    )


class TestDefaults:
    def test_no_rows_resolve_to_the_policy_default(self, session, world):
        resolved = resolve(session, world, [world["user"], world["peer"]])

        assert set(resolved) == {world["user"].id, world["peer"].id}
        for preference in resolved.values():
            assert preference.email_enabled is True
            assert preference.email_supported is True
            assert preference.is_explicit is False
            assert preference.updated_at is None

    def test_an_unsupported_type_defaults_to_disabled(self, session, world):
        resolved = resolve(session, world, [world["user"]], UNSUPPORTED)

        preference = resolved[world["user"].id]
        assert preference.email_supported is False
        assert preference.email_enabled is False

    def test_resolving_creates_no_row(self, session, world):
        resolve(session, world, [world["user"], world["peer"]])
        resolve_all_effective_preferences(
            session, tenant_id=world["tenant"].id, user_id=world["user"].id
        )

        assert session.exec(select(NotificationPreference)).all() == []


class TestOverrides:
    def test_one_explicit_override_is_honoured(self, session, world):
        override(session, world, world["user"], SUPPORTED, email=False)

        preference = resolve(session, world, [world["user"]])[world["user"].id]
        assert preference.email_enabled is False
        assert preference.is_explicit is True
        assert preference.updated_at is not None

    def test_an_override_applies_only_to_its_own_type(self, session, world):
        override(session, world, world["user"], SUPPORTED, email=False)

        other = resolve(
            session, world, [world["user"]], NotificationType.REPORT_SUBMITTED
        )[world["user"].id]
        assert other.email_enabled is True
        assert other.is_explicit is False

    def test_an_override_applies_only_to_its_own_user(self, session, world):
        override(session, world, world["user"], SUPPORTED, email=False)

        resolved = resolve(session, world, [world["user"], world["peer"]])
        assert resolved[world["user"].id].email_enabled is False
        assert resolved[world["peer"].id].email_enabled is True

    def test_policy_overrides_a_stale_row_that_enables_an_unsupported_type(
        self, session, world
    ):
        """The registry is the outer bound. A row saying `true` for a type
        whose policy says `email_supported = false` resolves to false — and
        is not rewritten, because a read must not mutate."""
        override(session, world, world["user"], UNSUPPORTED, email=True)

        preference = resolve(session, world, [world["user"]], UNSUPPORTED)[
            world["user"].id
        ]
        assert preference.email_enabled is False
        assert preference.is_explicit is True

        stored = session.exec(select(NotificationPreference)).all()
        assert len(stored) == 1
        assert stored[0].email_enabled is True

    def test_a_malformed_in_app_opt_out_resolves_to_enabled(self, session, world):
        override(session, world, world["user"], SUPPORTED, email=True, in_app=False)

        preference = resolve(session, world, [world["user"]])[world["user"].id]
        assert preference.in_app_enabled is True

        assert session.exec(select(NotificationPreference)).all()[0].in_app_enabled is False

    def test_the_malformed_row_is_logged_without_content(self, session, world, caplog):
        override(session, world, world["user"], SUPPORTED, email=True, in_app=False)

        with caplog.at_level("WARNING"):
            resolve(session, world, [world["user"]])

        records = [
            r
            for r in caplog.records
            if getattr(r, "event", None)
            == "notification.preference.invalid_in_app_disabled"
        ]
        assert records
        assert records[0].error_code == "in_app_disable_not_supported"


class TestScoping:
    def test_multiple_users_in_one_tenant_resolve_independently(
        self, session, world
    ):
        override(session, world, world["user"], SUPPORTED, email=False)
        override(session, world, world["third"], SUPPORTED, email=False)

        resolved = resolve(
            session, world, [world["user"], world["peer"], world["third"]]
        )
        assert resolved[world["user"].id].email_enabled is False
        assert resolved[world["peer"].id].email_enabled is True
        assert resolved[world["third"].id].email_enabled is False

    def test_a_user_from_another_tenant_is_omitted(self, session, world):
        """Dropped rather than raised on: this runs inside `notify()`, where
        the recipient set has already been tenant-validated, so this is
        defence in depth — an unvouched-for user simply resolves to nothing
        and therefore receives no delivery row."""
        resolved = resolve(session, world, [world["user"], world["stranger"]])

        assert set(resolved) == {world["user"].id}

    def test_another_tenants_override_never_leaks(self, session, world):
        override(session, world, world["stranger"], SUPPORTED, email=False)

        resolved = resolve_effective_notification_preferences(
            session,
            tenant_id=world["other_tenant"].id,
            user_ids=[world["stranger"].id],
            notification_type=SUPPORTED,
        )
        assert resolved[world["stranger"].id].email_enabled is False

        # The same user id resolved under the wrong tenant yields nothing.
        assert resolve(session, world, [world["stranger"]]) == {}

    def test_a_missing_user_is_omitted(self, session, world):
        ghost = uuid.uuid4()

        resolved = resolve_effective_notification_preferences(
            session,
            tenant_id=world["tenant"].id,
            user_ids=[world["user"].id, ghost],
            notification_type=SUPPORTED,
        )
        assert set(resolved) == {world["user"].id}

    def test_an_inactive_user_still_resolves(self, session, world):
        """Activity is a delivery-eligibility question, answered where the
        delivery is materialized — not a preference question. Conflating them
        here would mean reactivating a user silently changed what their
        preferences 'were'."""
        world["user"].is_active = False
        session.add(world["user"])
        session.commit()

        resolved = resolve(session, world, [world["user"]])
        assert resolved[world["user"].id].email_enabled is True

    def test_an_empty_user_list_resolves_to_nothing(self, session, world):
        assert resolve(session, world, []) == {}

    def test_duplicate_ids_resolve_once(self, session, world):
        resolved = resolve(session, world, [world["user"], world["user"]])
        assert set(resolved) == {world["user"].id}


def count_resolver_selects(session, call):
    """Statements the resolver itself issues, isolated from SQLAlchemy's own
    lazy refreshes of fixture objects the surrounding test committed."""
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))

    bind = session.get_bind()
    event.listen(bind, "before_cursor_execute", record)
    try:
        call()
    finally:
        event.remove(bind, "before_cursor_execute", record)

    return [
        statement
        for statement in statements
        if statement.upper().startswith("SELECT")
        and ("FROM notification_preference" in statement or "FROM app_user" in statement)
    ]


class TestQueryShape:
    """Every id is read *before* the statement listener attaches: SQLAlchemy
    expires persistent objects on commit, so touching `world["user"].id`
    inside the measured window would emit a refresh SELECT that belongs to
    the fixture, not to the resolver."""

    def test_resolution_is_not_n_plus_one(self, session, world):
        """Two statements regardless of how many users are passed: one for
        tenant-scoped user ids, one for whatever override rows exist."""
        tenant_id = world["tenant"].id
        user_ids = [world["user"].id, world["peer"].id, world["third"].id]

        selects = count_resolver_selects(
            session,
            lambda: resolve_effective_notification_preferences(
                session,
                tenant_id=tenant_id,
                user_ids=user_ids,
                notification_type=SUPPORTED,
            ),
        )
        assert len(selects) == 2, selects

    def test_the_query_count_does_not_grow_with_the_recipient_set(
        self, session, world
    ):
        """The property that actually matters: resolving three users costs
        exactly what resolving one costs. A per-recipient query would show up
        here even if the absolute count above were ever renegotiated."""
        tenant_id = world["tenant"].id
        one_id = [world["user"].id]
        three_ids = [world["user"].id, world["peer"].id, world["third"].id]

        def call(ids):
            return lambda: resolve_effective_notification_preferences(
                session,
                tenant_id=tenant_id,
                user_ids=ids,
                notification_type=SUPPORTED,
            )

        assert len(count_resolver_selects(session, call(one_id))) == len(
            count_resolver_selects(session, call(three_ids))
        )

    def test_results_are_deterministic(self, session, world):
        override(session, world, world["peer"], SUPPORTED, email=False)

        first = resolve(session, world, [world["user"], world["peer"]])
        second = resolve(session, world, [world["user"], world["peer"]])

        assert first == second


class TestAllTypes:
    def test_returns_every_type_in_a_stable_order(self, session, world):
        resolved = resolve_all_effective_preferences(
            session, tenant_id=world["tenant"].id, user_id=world["user"].id
        )

        assert [t for t, _ in resolved] == list(NOTIFICATION_TYPE_ORDER)
        assert list(NOTIFICATION_TYPE_ORDER) == list(NotificationType)

    def test_merges_explicit_rows_with_policy_defaults(self, session, world):
        override(session, world, world["user"], SUPPORTED, email=False)

        resolved = dict(
            resolve_all_effective_preferences(
                session, tenant_id=world["tenant"].id, user_id=world["user"].id
            )
        )

        assert resolved[SUPPORTED].email_enabled is False
        assert resolved[SUPPORTED].is_explicit is True
        assert resolved[NotificationType.REPORT_SUBMITTED].email_enabled is True
        assert resolved[NotificationType.REPORT_SUBMITTED].is_explicit is False
        assert resolved[UNSUPPORTED].email_supported is False

    def test_uses_one_query(self, session, world):
        tenant_id, user_id = world["tenant"].id, world["user"].id
        selects = count_resolver_selects(
            session,
            lambda: resolve_all_effective_preferences(
                session, tenant_id=tenant_id, user_id=user_id
            ),
        )
        assert len(selects) == 1, selects
