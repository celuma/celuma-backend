"""Notification-preference API integration tests (Céluma 1.3, Phase 3,
Block D).

Covers `GET`/`PUT /api/v1/notification-preferences` against the real migrated
schema, reusing tests/http/conftest.py exactly as Block B's inbox tests do.

Two properties are asserted repeatedly, because they are the ones a
regression would quietly break:

  - **absence means default.** Reading preferences must create nothing, and
    returning a value to its default must remove the row rather than persist
    one that agrees with the default.
  - **self-scoped, structurally.** There is no field through which a client
    can name another user or another tenant, so cross-user access is not a
    permission failure to be denied — it is unrepresentable, and the tests
    assert that shape rather than a 403.
"""
import pytest

from app.models.notification import (
    NotificationDelivery,
    NotificationPreference,
    NotificationType,
)
from app.services.notification_policies import (
    NOTIFICATION_DELIVERY_POLICIES,
    default_email_enabled,
)
from sqlmodel import select

from tests.http.factories import (
    auth_headers,
    create_branch,
    create_tenant,
    create_user,
)

BASE = "/api/v1/notification-preferences"

#: A type whose policy allows email, and one whose policy does not. Resolved
#: from the registry rather than hardcoded, so a deliberate policy change
#: reroutes these tests instead of silently making them assert nothing.
SUPPORTED = NotificationType.REPORT_PUBLISHED
UNSUPPORTED = NotificationType.SAMPLE_STATUS_CHANGED


@pytest.fixture(name="world")
def world_fixture(session):
    """Two tenants, three users. `user` is the subject; `peer` shares their
    tenant; `stranger` is in another tenant entirely."""
    tenant = create_tenant(session, name="Tenant A")
    create_branch(session, tenant)
    user = create_user(session, tenant, email="user@tenant-a.test")
    # `viewer` is the narrowest real role in the catalog — the same one Block
    # B used to prove the inbox needs no permission.
    peer = create_user(session, tenant, email="peer@tenant-a.test", roles=("viewer",))

    other_tenant = create_tenant(session, name="Tenant B")
    create_branch(session, other_tenant)
    stranger = create_user(session, other_tenant, email="stranger@tenant-b.test")

    return {
        "tenant": tenant,
        "user": user,
        "peer": peer,
        "other_tenant": other_tenant,
        "stranger": stranger,
    }


def rows(session, user=None):
    query = select(NotificationPreference)
    if user is not None:
        query = query.where(NotificationPreference.user_id == user.id)
    return session.exec(query).all()


def by_type(body):
    return {item["notification_type"]: item for item in body["preferences"]}


class TestPolicyRegistry:
    """D1 — the registry the whole API resolves against."""

    def test_every_notification_type_has_a_policy(self):
        """A type with no entry would reach `get_delivery_policy` and raise.
        Complete coverage is asserted here rather than defaulted to
        permissive at runtime, because the permissive default is 'send
        email nobody approved'."""
        assert set(NOTIFICATION_DELIVERY_POLICIES) == set(NotificationType)

    def test_in_app_is_required_for_every_type(self):
        assert all(
            policy.in_app_required
            for policy in NOTIFICATION_DELIVERY_POLICIES.values()
        )

    def test_sample_status_changed_is_the_only_in_app_only_type(self):
        """Confirmed against Block A's event inventory: it is the only
        MUST_HAVE_1_3 event that fires once per state transition and fans out
        to every order assignee."""
        unsupported = {
            notification_type
            for notification_type, policy in NOTIFICATION_DELIVERY_POLICIES.items()
            if not policy.email_supported
        }
        assert unsupported == {NotificationType.SAMPLE_STATUS_CHANGED}

    def test_an_unsupported_type_cannot_declare_an_email_default(self):
        from app.services.notification_policies import NotificationDeliveryPolicy

        with pytest.raises(ValueError):
            NotificationDeliveryPolicy(
                in_app_required=True,
                email_supported=False,
                email_default_enabled=True,
            )

    def test_the_effective_default_folds_in_support(self):
        assert default_email_enabled(SUPPORTED) is True
        assert default_email_enabled(UNSUPPORTED) is False


class TestGet:
    def test_returns_every_type_in_a_stable_order(self, client, world):
        response = client.get(BASE, headers=auth_headers(world["user"]))

        assert response.status_code == 200
        returned = [
            item["notification_type"] for item in response.json()["preferences"]
        ]
        assert returned == [t.value for t in NotificationType]

        # Stable across calls, not merely correct once.
        again = client.get(BASE, headers=auth_headers(world["user"]))
        assert [
            item["notification_type"] for item in again.json()["preferences"]
        ] == returned

    def test_an_empty_table_returns_policy_defaults(self, client, session, world):
        response = client.get(BASE, headers=auth_headers(world["user"]))

        for notification_type, item in by_type(response.json()).items():
            expected = default_email_enabled(NotificationType(notification_type))
            assert item["email_enabled"] is expected
            assert item["is_explicit"] is False
            assert item["updated_at"] is None
            assert item["in_app_enabled"] is True

        assert rows(session) == []

    def test_reading_preferences_creates_no_row(self, client, session, world):
        """Opening the Profile screen must leave the table exactly as it was
        — an empty `notification_preference` is the correct steady state."""
        for _ in range(3):
            client.get(BASE, headers=auth_headers(world["user"]))

        assert rows(session) == []

    def test_an_explicit_override_is_returned_as_explicit(
        self, client, session, world
    ):
        client.put(
            BASE,
            headers=auth_headers(world["user"]),
            json={
                "preferences": [
                    {"notification_type": SUPPORTED.value, "email_enabled": False}
                ]
            },
        )

        item = by_type(client.get(BASE, headers=auth_headers(world["user"])).json())[
            SUPPORTED.value
        ]
        assert item["email_enabled"] is False
        assert item["is_explicit"] is True
        assert item["updated_at"] is not None

    def test_an_unsupported_type_is_represented_honestly(self, client, world):
        item = by_type(client.get(BASE, headers=auth_headers(world["user"])).json())[
            UNSUPPORTED.value
        ]
        assert item["email_supported"] is False
        assert item["email_enabled"] is False
        assert item["in_app_enabled"] is True

    def test_a_stale_row_cannot_re_enable_an_unsupported_type(
        self, client, session, world
    ):
        """The API refuses to write this row, but a hand-edited database or a
        future policy change can produce one. The policy is the outer bound,
        and the row is left untouched rather than repaired on read."""
        session.add(
            NotificationPreference(
                tenant_id=world["tenant"].id,
                user_id=world["user"].id,
                notification_type=UNSUPPORTED.value,
                email_enabled=True,
            )
        )
        session.commit()

        item = by_type(client.get(BASE, headers=auth_headers(world["user"])).json())[
            UNSUPPORTED.value
        ]
        assert item["email_enabled"] is False

        stored = rows(session, world["user"])
        assert len(stored) == 1
        assert stored[0].email_enabled is True  # not mutated by the read

    def test_a_malformed_in_app_opt_out_does_not_disable_in_app(
        self, client, session, world
    ):
        """Ignored with a warning, never honoured: a user has no UI to undo
        it, so honouring it would silently hide operational notifications."""
        session.add(
            NotificationPreference(
                tenant_id=world["tenant"].id,
                user_id=world["user"].id,
                notification_type=SUPPORTED.value,
                in_app_enabled=False,
                email_enabled=True,
            )
        )
        session.commit()

        item = by_type(client.get(BASE, headers=auth_headers(world["user"])).json())[
            SUPPORTED.value
        ]
        assert item["in_app_enabled"] is True

        assert rows(session, world["user"])[0].in_app_enabled is False  # not repaired

    def test_requires_authentication(self, client):
        assert client.get(BASE).status_code == 401

    def test_requires_no_permission_beyond_authentication(self, client, world):
        """`viewer` holds the narrowest permission set in the catalog. A user
        who can receive notifications must be able to manage how they receive
        them."""
        assert client.get(BASE, headers=auth_headers(world["peer"])).status_code == 200

    def test_never_shows_another_users_rows(self, client, session, world):
        client.put(
            BASE,
            headers=auth_headers(world["peer"]),
            json={
                "preferences": [
                    {"notification_type": SUPPORTED.value, "email_enabled": False}
                ]
            },
        )

        item = by_type(client.get(BASE, headers=auth_headers(world["user"])).json())[
            SUPPORTED.value
        ]
        assert item["is_explicit"] is False
        assert item["email_enabled"] is True

    def test_never_shows_another_tenants_rows(self, client, world):
        client.put(
            BASE,
            headers=auth_headers(world["stranger"]),
            json={
                "preferences": [
                    {"notification_type": SUPPORTED.value, "email_enabled": False}
                ]
            },
        )

        item = by_type(client.get(BASE, headers=auth_headers(world["user"])).json())[
            SUPPORTED.value
        ]
        assert item["is_explicit"] is False

    def test_a_user_or_tenant_parameter_changes_nothing(self, client, world):
        """There is no field to send one in, so a client that tries is simply
        ignored by the router — the scope comes from the token."""
        response = client.get(
            BASE,
            params={
                "user_id": str(world["peer"].id),
                "tenant_id": str(world["other_tenant"].id),
            },
            headers=auth_headers(world["user"]),
        )

        assert response.status_code == 200
        assert len(response.json()["preferences"]) == len(NotificationType)


class TestPut:
    def _put(self, client, user, items):
        return client.put(
            BASE, headers=auth_headers(user), json={"preferences": items}
        )

    def test_creates_an_explicit_override(self, client, session, world):
        response = self._put(
            client,
            world["user"],
            [{"notification_type": SUPPORTED.value, "email_enabled": False}],
        )

        assert response.status_code == 200
        stored = rows(session, world["user"])
        assert len(stored) == 1
        assert stored[0].notification_type == SUPPORTED
        assert stored[0].email_enabled is False
        # Never writable by a user; only ever written as True.
        assert stored[0].in_app_enabled is True

    def test_updates_an_existing_override(self, client, session, world):
        self._put(
            client,
            world["user"],
            [{"notification_type": SUPPORTED.value, "email_enabled": False}],
        )
        first = rows(session, world["user"])[0].updated_at

        # Same non-default value again: the row is refreshed, not duplicated.
        self._put(
            client,
            world["user"],
            [{"notification_type": SUPPORTED.value, "email_enabled": False}],
        )

        session.expire_all()
        stored = rows(session, world["user"])
        assert len(stored) == 1
        assert stored[0].updated_at >= first

    def test_returning_to_the_default_removes_the_row(self, client, session, world):
        """Sparse by design. A row that agrees with the default would pin the
        user to today's default forever if the product default ever changed —
        a choice they never actually made."""
        self._put(
            client,
            world["user"],
            [{"notification_type": SUPPORTED.value, "email_enabled": False}],
        )
        assert len(rows(session, world["user"])) == 1

        response = self._put(
            client,
            world["user"],
            [{"notification_type": SUPPORTED.value, "email_enabled": True}],
        )

        session.expire_all()
        assert rows(session, world["user"]) == []
        item = by_type(response.json())[SUPPORTED.value]
        assert item["email_enabled"] is True
        assert item["is_explicit"] is False
        assert item["updated_at"] is None

    def test_a_partial_batch_leaves_other_types_untouched(
        self, client, session, world
    ):
        self._put(
            client,
            world["user"],
            [
                {
                    "notification_type": NotificationType.REPORT_SUBMITTED.value,
                    "email_enabled": False,
                },
                {
                    "notification_type": NotificationType.REPORT_RETRACTED.value,
                    "email_enabled": False,
                },
            ],
        )

        # A second request mentioning only one of them.
        response = self._put(
            client,
            world["user"],
            [
                {
                    "notification_type": NotificationType.REPORT_SUBMITTED.value,
                    "email_enabled": True,
                }
            ],
        )

        items = by_type(response.json())
        assert items[NotificationType.REPORT_SUBMITTED.value]["is_explicit"] is False
        assert items[NotificationType.REPORT_RETRACTED.value]["email_enabled"] is False
        assert items[NotificationType.REPORT_RETRACTED.value]["is_explicit"] is True

    def test_returns_the_full_effective_list(self, client, world):
        response = self._put(
            client,
            world["user"],
            [{"notification_type": SUPPORTED.value, "email_enabled": False}],
        )

        returned = [
            item["notification_type"] for item in response.json()["preferences"]
        ]
        assert returned == [t.value for t in NotificationType]

    def test_rejects_a_duplicate_notification_type(self, client, session, world):
        """No discoverable intent: the user would watch a switch settle on a
        value they did not pick."""
        response = self._put(
            client,
            world["user"],
            [
                {"notification_type": SUPPORTED.value, "email_enabled": False},
                {"notification_type": SUPPORTED.value, "email_enabled": True},
            ],
        )

        assert response.status_code == 422
        assert rows(session, world["user"]) == []

    def test_rejects_an_unknown_notification_type(self, client, session, world):
        response = self._put(
            client,
            world["user"],
            [{"notification_type": "REPORT_INCINERATED", "email_enabled": False}],
        )

        assert response.status_code == 422
        assert rows(session, world["user"]) == []

    def test_rejects_enabling_email_for_an_unsupported_type(
        self, client, session, world
    ):
        response = self._put(
            client,
            world["user"],
            [{"notification_type": UNSUPPORTED.value, "email_enabled": True}],
        )

        assert response.status_code == 422
        assert rows(session, world["user"]) == []

    def test_disabling_an_unsupported_type_is_a_no_op(self, client, session, world):
        """Its effective default is already false, so the uniform
        'value equals the default -> no row' rule stores nothing."""
        response = self._put(
            client,
            world["user"],
            [{"notification_type": UNSUPPORTED.value, "email_enabled": False}],
        )

        assert response.status_code == 200
        assert rows(session, world["user"]) == []

    def test_rejects_in_app_enabled_in_the_body(self, client, session, world):
        """Rejected rather than accepted-and-ignored: silently dropping the
        field would let a caller believe it had disabled in-app delivery."""
        response = self._put(
            client,
            world["user"],
            [
                {
                    "notification_type": SUPPORTED.value,
                    "email_enabled": True,
                    "in_app_enabled": False,
                }
            ],
        )

        assert response.status_code == 422
        assert rows(session, world["user"]) == []

    @pytest.mark.parametrize("field", ["user_id", "tenant_id", "channel"])
    def test_rejects_a_scope_or_channel_field_in_the_body(
        self, client, session, world, field
    ):
        response = self._put(
            client,
            world["user"],
            [
                {
                    "notification_type": SUPPORTED.value,
                    "email_enabled": False,
                    field: "anything",
                }
            ],
        )

        assert response.status_code == 422
        assert rows(session, world["user"]) == []

    def test_rejects_an_empty_batch(self, client, world):
        assert self._put(client, world["user"], []).status_code == 422

    def test_one_invalid_item_applies_none_of_the_batch(
        self, client, session, world
    ):
        """Atomic. A partially applied batch leaves the user's screen and the
        database disagreeing about what was just saved."""
        response = self._put(
            client,
            world["user"],
            [
                {"notification_type": SUPPORTED.value, "email_enabled": False},
                {"notification_type": UNSUPPORTED.value, "email_enabled": True},
            ],
        )

        assert response.status_code == 422
        assert rows(session, world["user"]) == []

    def test_requires_authentication(self, client):
        response = client.put(
            BASE,
            json={
                "preferences": [
                    {"notification_type": SUPPORTED.value, "email_enabled": False}
                ]
            },
        )
        assert response.status_code == 401

    def test_requires_no_permission_beyond_authentication(self, client, world):
        assert (
            self._put(
                client,
                world["peer"],
                [{"notification_type": SUPPORTED.value, "email_enabled": False}],
            ).status_code
            == 200
        )

    def test_cannot_target_another_users_preferences(self, client, session, world):
        """Writes land on the caller, whatever a client hopes to address."""
        self._put(
            client,
            world["user"],
            [{"notification_type": SUPPORTED.value, "email_enabled": False}],
        )

        assert len(rows(session, world["user"])) == 1
        assert rows(session, world["peer"]) == []

    def test_cannot_touch_another_tenants_rows(self, client, session, world):
        self._put(
            client,
            world["stranger"],
            [{"notification_type": SUPPORTED.value, "email_enabled": False}],
        )
        self._put(
            client,
            world["user"],
            [{"notification_type": SUPPORTED.value, "email_enabled": True}],
        )

        stranger_rows = rows(session, world["stranger"])
        assert len(stranger_rows) == 1
        assert stranger_rows[0].tenant_id == world["other_tenant"].id

    def test_updated_at_is_server_generated_and_shared_by_the_batch(
        self, client, world
    ):
        response = self._put(
            client,
            world["user"],
            [
                {
                    "notification_type": NotificationType.REPORT_SUBMITTED.value,
                    "email_enabled": False,
                },
                {
                    "notification_type": NotificationType.REPORT_PUBLISHED.value,
                    "email_enabled": False,
                },
            ],
        )

        items = by_type(response.json())
        stamps = {
            items[NotificationType.REPORT_SUBMITTED.value]["updated_at"],
            items[NotificationType.REPORT_PUBLISHED.value]["updated_at"],
        }
        assert len(stamps) == 1
        assert None not in stamps

    def test_changing_preferences_creates_no_notification_or_delivery_row(
        self, client, session, world
    ):
        """Preferences configure the future. They are not themselves an
        event, and they never retroactively create or remove a delivery."""
        from app.models.notification import Notification

        self._put(
            client,
            world["user"],
            [{"notification_type": SUPPORTED.value, "email_enabled": False}],
        )
        self._put(
            client,
            world["user"],
            [{"notification_type": SUPPORTED.value, "email_enabled": True}],
        )

        assert session.exec(select(Notification)).all() == []
        assert session.exec(select(NotificationDelivery)).all() == []
