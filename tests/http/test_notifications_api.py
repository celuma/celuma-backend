"""Notification inbox API integration tests (Céluma 1.3, Phase 3, Block B).

Covers the four Block B endpoints end to end against the real migrated
schema, using the existing tests/http/conftest.py infrastructure — no new
test framework, per the Block A implementation plan.

The recurring shape of these tests is "user A must not see or touch user B's
row, and neither may reach across tenants", asserted as 404 rather than 403
throughout: this codebase does not confirm to a caller that a resource exists
somewhere they cannot see it.
"""
import uuid
from datetime import datetime, timedelta

import pytest

from app.models.notification import NotificationRecipientStatus, NotificationType
from tests.http.factories import (
    auth_headers,
    create_branch,
    create_inbox_notification,
    create_notification,
    create_recipient,
    create_tenant,
    create_user,
)

BASE = "/api/v1/notifications"


@pytest.fixture(name="world")
def world_fixture(session):
    """Two tenants, three users. `user` is the subject of every assertion;
    `peer` shares their tenant; `stranger` is in another tenant entirely."""
    tenant = create_tenant(session, name="Tenant A")
    create_branch(session, tenant)
    user = create_user(session, tenant, email="user@tenant-a.test")
    peer = create_user(session, tenant, email="peer@tenant-a.test")

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


def seed(session, world, *, owner=None, count=1, **kwargs):
    """`count` notifications addressed to `owner`, oldest first."""
    owner = owner or world["user"]
    tenant = world["other_tenant"] if owner is world["stranger"] else world["tenant"]
    base_time = kwargs.pop("base_time", datetime(2026, 8, 1, 12, 0, 0))
    created = []
    for index in range(count):
        created.append(
            create_inbox_notification(
                session,
                tenant,
                owner,
                created_at=base_time + timedelta(minutes=index),
                **kwargs,
            )
        )
    return created


class TestList:
    def test_returns_the_callers_own_rows(self, client, session, world):
        seed(session, world, count=2)

        response = client.get(BASE, headers=auth_headers(world["user"]))

        assert response.status_code == 200
        body = response.json()
        assert len(body["items"]) == 2
        assert body["next_cursor"] is None

    def test_response_names_recipient_id_and_notification_id_distinctly(
        self, client, session, world
    ):
        """The Block A proposal used `id` ambiguously. The read endpoint acts
        on the recipient id, so conflating the two would let a client send
        the shared notification id and get an unexplained 404."""
        notification, recipient = seed(session, world)[0]

        item = client.get(BASE, headers=auth_headers(world["user"])).json()["items"][0]

        assert item["recipient_id"] == str(recipient.id)
        assert item["notification_id"] == str(notification.id)
        assert "id" not in item
        assert recipient.id != notification.id

    def test_item_carries_the_frozen_content_and_deep_link_fields(
        self, client, session, world
    ):
        notification, _ = seed(session, world)[0]

        item = client.get(BASE, headers=auth_headers(world["user"])).json()["items"][0]

        assert item["title"] == notification.title
        assert item["body"] == notification.body
        assert item["type"] == "REPORT_SUBMITTED"
        assert item["severity"] == "INFO"
        assert item["resource_type"] == "report"
        assert item["resource_id"] == str(notification.resource_id)
        assert item["status"] == "UNREAD"
        assert item["read_at"] is None

    def test_raw_metadata_is_never_exposed(self, client, session, world):
        """Content policy §6: `notification_metadata` is an internal audit
        field. The deep-link data a client legitimately needs is already
        promoted to top-level fields."""
        seed(
            session,
            world,
            metadata={
                "template_key": "report_submitted_v1",
                "template_params": {"order_number": "ORD-1"},
                "reviewer_snapshot": ["u1", "u2"],
            },
        )

        item = client.get(BASE, headers=auth_headers(world["user"])).json()["items"][0]

        assert "notification_metadata" not in item
        assert "metadata" not in item
        assert "template_key" not in item
        assert "reviewer_snapshot" not in str(item)

    def test_does_not_return_another_users_rows(self, client, session, world):
        seed(session, world, owner=world["peer"], count=3)

        body = client.get(BASE, headers=auth_headers(world["user"])).json()

        assert body["items"] == []

    def test_does_not_return_another_tenants_rows(self, client, session, world):
        seed(session, world, owner=world["stranger"], count=2)

        body = client.get(BASE, headers=auth_headers(world["user"])).json()

        assert body["items"] == []

    def test_a_shared_notification_shows_only_the_callers_own_recipient_row(
        self, client, session, world
    ):
        """One event, two recipients — each user sees their own row, with
        their own read state, never the other's."""
        notification = create_notification(session, world["tenant"])
        mine = create_recipient(session, notification, world["user"])
        create_recipient(session, notification, world["peer"], status="READ",
                         read_at=datetime.utcnow())

        items = client.get(BASE, headers=auth_headers(world["user"])).json()["items"]

        assert len(items) == 1
        assert items[0]["recipient_id"] == str(mine.id)
        assert items[0]["status"] == "UNREAD"

    def test_ordering_is_newest_first(self, client, session, world):
        seed(session, world, count=4)

        items = client.get(BASE, headers=auth_headers(world["user"])).json()["items"]

        timestamps = [item["created_at"] for item in items]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_pagination_walks_every_row_exactly_once(self, client, session, world):
        seed(session, world, count=7)
        headers = auth_headers(world["user"])

        seen, cursor, pages = [], None, 0
        while True:
            params = {"limit": 3}
            if cursor:
                params["cursor"] = cursor
            body = client.get(BASE, params=params, headers=headers).json()
            seen.extend(item["recipient_id"] for item in body["items"])
            pages += 1
            cursor = body["next_cursor"]
            if not cursor:
                break
            assert pages < 10, "cursor did not terminate"

        assert len(seen) == 7
        assert len(set(seen)) == 7

    def test_cursor_is_stable_when_timestamps_collide(self, client, session, world):
        """Several notifications created in one transaction share a timestamp
        to the microsecond. Without the secondary sort on recipient id, a page
        boundary landing inside that group would skip or repeat rows."""
        identical = datetime(2026, 8, 1, 12, 0, 0)
        for _ in range(6):
            create_inbox_notification(
                session, world["tenant"], world["user"], created_at=identical
            )
        headers = auth_headers(world["user"])

        first = client.get(BASE, params={"limit": 3}, headers=headers).json()
        second = client.get(
            BASE,
            params={"limit": 3, "cursor": first["next_cursor"]},
            headers=headers,
        ).json()

        first_ids = {item["recipient_id"] for item in first["items"]}
        second_ids = {item["recipient_id"] for item in second["items"]}
        assert len(first_ids) == 3
        assert len(second_ids) == 3
        assert first_ids.isdisjoint(second_ids)

    def test_next_cursor_is_null_on_the_last_page(self, client, session, world):
        seed(session, world, count=2)

        body = client.get(
            BASE, params={"limit": 5}, headers=auth_headers(world["user"])
        ).json()

        assert len(body["items"]) == 2
        assert body["next_cursor"] is None

    def test_an_invalid_cursor_is_rejected(self, client, session, world):
        seed(session, world)

        response = client.get(
            BASE, params={"cursor": "not-a-cursor"}, headers=auth_headers(world["user"])
        )

        assert response.status_code == 400

    @pytest.mark.parametrize("limit", [0, -1, 101])
    def test_limit_bounds_are_enforced(self, client, session, world, limit):
        response = client.get(
            BASE, params={"limit": limit}, headers=auth_headers(world["user"])
        )
        assert response.status_code == 400

    def test_maximum_limit_is_accepted(self, client, session, world):
        seed(session, world, count=2)

        response = client.get(
            BASE, params={"limit": 100}, headers=auth_headers(world["user"])
        )

        assert response.status_code == 200
        assert len(response.json()["items"]) == 2

    def test_unread_only_filter(self, client, session, world):
        seed(session, world, count=2)
        seed(
            session,
            world,
            count=3,
            status="READ",
            read_at=datetime.utcnow(),
            base_time=datetime(2026, 7, 1, 12, 0, 0),
        )
        headers = auth_headers(world["user"])

        assert len(client.get(BASE, headers=headers).json()["items"]) == 5
        unread = client.get(
            BASE, params={"unread_only": "true"}, headers=headers
        ).json()["items"]
        assert len(unread) == 2
        assert {item["status"] for item in unread} == {"UNREAD"}

    def test_single_type_filter(self, client, session, world):
        seed(session, world, notification_type="REPORT_SUBMITTED")
        seed(session, world, notification_type="REPORT_PUBLISHED")
        seed(session, world, notification_type="SAMPLE_STATUS_CHANGED")

        items = client.get(
            BASE,
            params={"type": "REPORT_PUBLISHED"},
            headers=auth_headers(world["user"]),
        ).json()["items"]

        assert [item["type"] for item in items] == ["REPORT_PUBLISHED"]

    def test_repeated_type_filter_is_an_or(self, client, session, world):
        seed(session, world, notification_type="REPORT_SUBMITTED")
        seed(session, world, notification_type="REPORT_PUBLISHED")
        seed(session, world, notification_type="SAMPLE_STATUS_CHANGED")

        items = client.get(
            BASE,
            params=[("type", "REPORT_SUBMITTED"), ("type", "SAMPLE_STATUS_CHANGED")],
            headers=auth_headers(world["user"]),
        ).json()["items"]

        assert {item["type"] for item in items} == {
            "REPORT_SUBMITTED",
            "SAMPLE_STATUS_CHANGED",
        }

    def test_an_unknown_type_value_is_rejected(self, client, session, world):
        response = client.get(
            BASE, params={"type": "NOT_A_TYPE"}, headers=auth_headers(world["user"])
        )
        assert response.status_code == 422

    def test_date_range_filter(self, client, session, world):
        create_inbox_notification(
            session, world["tenant"], world["user"], created_at=datetime(2026, 1, 1)
        )
        create_inbox_notification(
            session, world["tenant"], world["user"], created_at=datetime(2026, 6, 15)
        )
        create_inbox_notification(
            session, world["tenant"], world["user"], created_at=datetime(2026, 12, 31)
        )
        headers = auth_headers(world["user"])

        items = client.get(
            BASE,
            params={"since": "2026-06-01T00:00:00", "until": "2026-07-01T00:00:00"},
            headers=headers,
        ).json()["items"]
        assert len(items) == 1

        assert (
            len(client.get(BASE, params={"since": "2026-06-01T00:00:00"}, headers=headers).json()["items"])
            == 2
        )
        assert (
            len(client.get(BASE, params={"until": "2026-06-01T00:00:00"}, headers=headers).json()["items"])
            == 1
        )

    def test_an_inverted_date_range_is_rejected(self, client, session, world):
        response = client.get(
            BASE,
            params={"since": "2026-07-01T00:00:00", "until": "2026-06-01T00:00:00"},
            headers=auth_headers(world["user"]),
        )
        assert response.status_code == 400

    def test_filters_combine(self, client, session, world):
        seed(session, world, notification_type="REPORT_PUBLISHED", status="READ",
             read_at=datetime.utcnow())
        seed(session, world, notification_type="REPORT_PUBLISHED")
        seed(session, world, notification_type="REPORT_SUBMITTED")

        items = client.get(
            BASE,
            params={"type": "REPORT_PUBLISHED", "unread_only": "true"},
            headers=auth_headers(world["user"]),
        ).json()["items"]

        assert len(items) == 1
        assert items[0]["type"] == "REPORT_PUBLISHED"
        assert items[0]["status"] == "UNREAD"

    def test_an_empty_inbox_returns_an_empty_list(self, client, session, world):
        body = client.get(BASE, headers=auth_headers(world["user"])).json()

        assert body == {"items": [], "next_cursor": None}


class TestUnreadCount:
    def test_counts_only_unread_rows(self, client, session, world):
        seed(session, world, count=4)
        seed(session, world, count=2, status="READ", read_at=datetime.utcnow(),
             base_time=datetime(2026, 7, 1, 12, 0, 0))

        response = client.get(f"{BASE}/unread-count", headers=auth_headers(world["user"]))

        assert response.status_code == 200
        assert response.json() == {"unread_count": 4}

    def test_excludes_other_users(self, client, session, world):
        seed(session, world, owner=world["peer"], count=5)
        seed(session, world, count=1)

        body = client.get(
            f"{BASE}/unread-count", headers=auth_headers(world["user"])
        ).json()

        assert body["unread_count"] == 1

    def test_excludes_other_tenants(self, client, session, world):
        seed(session, world, owner=world["stranger"], count=3)

        body = client.get(
            f"{BASE}/unread-count", headers=auth_headers(world["user"])
        ).json()

        assert body["unread_count"] == 0

    def test_zero_for_an_empty_inbox(self, client, session, world):
        body = client.get(
            f"{BASE}/unread-count", headers=auth_headers(world["user"])
        ).json()

        assert body["unread_count"] == 0


class TestMarkOneAsRead:
    def test_marks_an_unread_row_read_and_stamps_the_time(self, client, session, world):
        _, recipient = seed(session, world)[0]

        response = client.post(
            f"{BASE}/{recipient.id}/read", headers=auth_headers(world["user"])
        )

        assert response.status_code == 200
        body = response.json()
        assert body["recipient_id"] == str(recipient.id)
        assert body["status"] == "READ"
        assert body["read_at"] is not None

    def test_the_change_is_reflected_in_the_unread_count(self, client, session, world):
        _, recipient = seed(session, world, count=1)[0]
        headers = auth_headers(world["user"])
        assert client.get(f"{BASE}/unread-count", headers=headers).json()["unread_count"] == 1

        client.post(f"{BASE}/{recipient.id}/read", headers=headers)

        assert client.get(f"{BASE}/unread-count", headers=headers).json()["unread_count"] == 0

    def test_a_second_call_is_idempotent(self, client, session, world):
        """Two browser tabs marking the same row read is ordinary, not
        exceptional — the repeat returns 200 with the unchanged state."""
        _, recipient = seed(session, world)[0]
        headers = auth_headers(world["user"])

        first = client.post(f"{BASE}/{recipient.id}/read", headers=headers).json()
        second = client.post(f"{BASE}/{recipient.id}/read", headers=headers)

        assert second.status_code == 200
        assert second.json()["status"] == "READ"
        # The original timestamp is not moved by the repeat.
        assert second.json()["read_at"] == first["read_at"]

    def test_another_users_recipient_id_returns_404(self, client, session, world):
        _, peers_row = seed(session, world, owner=world["peer"])[0]

        response = client.post(
            f"{BASE}/{peers_row.id}/read", headers=auth_headers(world["user"])
        )

        assert response.status_code == 404
        session.refresh(peers_row)
        assert peers_row.status == NotificationRecipientStatus.UNREAD

    def test_another_tenants_recipient_id_returns_404(self, client, session, world):
        _, strangers_row = seed(session, world, owner=world["stranger"])[0]

        response = client.post(
            f"{BASE}/{strangers_row.id}/read", headers=auth_headers(world["user"])
        )

        assert response.status_code == 404
        session.refresh(strangers_row)
        assert strangers_row.status == NotificationRecipientStatus.UNREAD

    def test_an_unknown_id_returns_404(self, client, session, world):
        response = client.post(
            f"{BASE}/{uuid.uuid4()}/read", headers=auth_headers(world["user"])
        )
        assert response.status_code == 404

    def test_a_malformed_id_returns_404_not_422(self, client, session, world):
        """404 rather than a validation error: answering 422 would confirm the
        shape of the id space to an unauthorized caller."""
        response = client.post(
            f"{BASE}/not-a-uuid/read", headers=auth_headers(world["user"])
        )
        assert response.status_code == 404

    def test_the_shared_notification_id_is_not_accepted(self, client, session, world):
        """The path id is a NotificationRecipient.id. Passing the shared
        Notification.id must not work — read state is per-user."""
        notification, _ = seed(session, world)[0]

        response = client.post(
            f"{BASE}/{notification.id}/read", headers=auth_headers(world["user"])
        )

        assert response.status_code == 404

    def test_marking_read_does_not_affect_another_recipient_of_the_same_event(
        self, client, session, world
    ):
        notification = create_notification(session, world["tenant"])
        mine = create_recipient(session, notification, world["user"])
        theirs = create_recipient(session, notification, world["peer"])

        client.post(f"{BASE}/{mine.id}/read", headers=auth_headers(world["user"]))

        session.refresh(theirs)
        assert theirs.status == NotificationRecipientStatus.UNREAD
        assert theirs.read_at is None


class TestMarkAllAsRead:
    def test_marks_every_unread_row(self, client, session, world):
        seed(session, world, count=5)
        headers = auth_headers(world["user"])

        response = client.post(f"{BASE}/read-all", headers=headers)

        assert response.status_code == 200
        assert response.json() == {"updated_count": 5}
        assert client.get(f"{BASE}/unread-count", headers=headers).json()["unread_count"] == 0

    def test_a_repeated_request_updates_nothing(self, client, session, world):
        seed(session, world, count=3)
        headers = auth_headers(world["user"])

        client.post(f"{BASE}/read-all", headers=headers)
        second = client.post(f"{BASE}/read-all", headers=headers)

        assert second.json() == {"updated_count": 0}

    def test_already_read_rows_are_not_re_stamped(self, client, session, world):
        original = datetime(2026, 1, 1, 9, 0, 0)
        _, already_read = seed(
            session, world, status="READ", read_at=original
        )[0]
        seed(session, world, count=2, base_time=datetime(2026, 8, 2, 12, 0, 0))

        response = client.post(f"{BASE}/read-all", headers=auth_headers(world["user"]))

        assert response.json() == {"updated_count": 2}
        session.refresh(already_read)
        assert already_read.read_at == original

    def test_type_filter_narrows_the_operation(self, client, session, world):
        seed(session, world, notification_type="REPORT_PUBLISHED", count=2)
        seed(session, world, notification_type="REPORT_SUBMITTED", count=3,
             base_time=datetime(2026, 8, 2, 12, 0, 0))
        headers = auth_headers(world["user"])

        response = client.post(
            f"{BASE}/read-all", params={"type": "REPORT_PUBLISHED"}, headers=headers
        )

        assert response.json() == {"updated_count": 2}
        assert client.get(f"{BASE}/unread-count", headers=headers).json()["unread_count"] == 3

    def test_repeated_type_filter_is_an_or(self, client, session, world):
        seed(session, world, notification_type="REPORT_PUBLISHED")
        seed(session, world, notification_type="REPORT_SUBMITTED",
             base_time=datetime(2026, 8, 2, 12, 0, 0))
        seed(session, world, notification_type="SAMPLE_STATUS_CHANGED",
             base_time=datetime(2026, 8, 3, 12, 0, 0))

        response = client.post(
            f"{BASE}/read-all",
            params=[("type", "REPORT_PUBLISHED"), ("type", "REPORT_SUBMITTED")],
            headers=auth_headers(world["user"]),
        )

        assert response.json() == {"updated_count": 2}

    def test_date_range_filter_narrows_the_operation(self, client, session, world):
        create_inbox_notification(
            session, world["tenant"], world["user"], created_at=datetime(2026, 1, 1)
        )
        create_inbox_notification(
            session, world["tenant"], world["user"], created_at=datetime(2026, 6, 15)
        )
        create_inbox_notification(
            session, world["tenant"], world["user"], created_at=datetime(2026, 12, 31)
        )

        response = client.post(
            f"{BASE}/read-all",
            params={"since": "2026-06-01T00:00:00", "until": "2026-07-01T00:00:00"},
            headers=auth_headers(world["user"]),
        )

        assert response.json() == {"updated_count": 1}

    def test_an_inverted_date_range_is_rejected(self, client, session, world):
        response = client.post(
            f"{BASE}/read-all",
            params={"since": "2026-07-01T00:00:00", "until": "2026-06-01T00:00:00"},
            headers=auth_headers(world["user"]),
        )
        assert response.status_code == 400

    def test_does_not_modify_another_users_rows(self, client, session, world):
        _, peers_row = seed(session, world, owner=world["peer"])[0]
        seed(session, world, count=1)

        response = client.post(f"{BASE}/read-all", headers=auth_headers(world["user"]))

        assert response.json() == {"updated_count": 1}
        session.refresh(peers_row)
        assert peers_row.status == NotificationRecipientStatus.UNREAD

    def test_does_not_modify_another_tenants_rows(self, client, session, world):
        _, strangers_row = seed(session, world, owner=world["stranger"])[0]

        response = client.post(f"{BASE}/read-all", headers=auth_headers(world["user"]))

        assert response.json() == {"updated_count": 0}
        session.refresh(strangers_row)
        assert strangers_row.status == NotificationRecipientStatus.UNREAD

    def test_all_affected_rows_share_one_read_timestamp(self, client, session, world):
        """One server-generated timestamp for the whole statement, so "the
        batch I dismissed at 14:32" groups correctly later."""
        rows = [recipient for _, recipient in seed(session, world, count=3)]

        client.post(f"{BASE}/read-all", headers=auth_headers(world["user"]))

        for row in rows:
            session.refresh(row)
        assert len({row.read_at for row in rows}) == 1

    def test_an_empty_inbox_returns_zero(self, client, session, world):
        response = client.post(f"{BASE}/read-all", headers=auth_headers(world["user"]))
        assert response.json() == {"updated_count": 0}


class TestAuthentication:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", BASE),
            ("get", f"{BASE}/unread-count"),
            ("post", f"{BASE}/read-all"),
            ("post", f"{BASE}/{uuid.uuid4()}/read"),
        ],
    )
    def test_unauthenticated_requests_return_401(self, client, method, path):
        response = getattr(client, method)(path)
        assert response.status_code == 401

    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", BASE),
            ("get", f"{BASE}/unread-count"),
            ("post", f"{BASE}/read-all"),
        ],
    )
    def test_an_invalid_token_returns_401(self, client, method, path):
        response = getattr(client, method)(
            path, headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert response.status_code == 401

    def test_a_user_with_no_extra_permission_can_use_their_own_inbox(
        self, client, session, world
    ):
        """The inbox endpoints are self-scoping, so they carry no RBAC gate
        beyond authentication (Block A API proposal §3). `viewer` is the
        narrowest real role in the catalog — if it works, no permission is
        being required implicitly."""
        minimal = create_user(
            session, world["tenant"], email="viewer@tenant-a.test", roles=("viewer",)
        )
        _, recipient = create_inbox_notification(session, world["tenant"], minimal)
        headers = auth_headers(minimal)

        assert client.get(BASE, headers=headers).status_code == 200
        assert client.get(f"{BASE}/unread-count", headers=headers).status_code == 200
        assert client.post(f"{BASE}/{recipient.id}/read", headers=headers).status_code == 200
        assert client.post(f"{BASE}/read-all", headers=headers).status_code == 200

    def test_no_endpoint_accepts_a_tenant_or_user_parameter(
        self, client, session, world
    ):
        """Scope comes from the token, never the client. Passing someone
        else's ids must not widen what is returned."""
        seed(session, world, owner=world["peer"], count=3)
        seed(session, world, owner=world["stranger"], count=3)

        body = client.get(
            BASE,
            params={
                "user_id": str(world["peer"].id),
                "tenant_id": str(world["other_tenant"].id),
            },
            headers=auth_headers(world["user"]),
        ).json()

        assert body["items"] == []


class TestApiSurface:
    def test_only_the_four_inbox_and_two_preference_endpoints_exist(self, client):
        """Céluma 1.3 Phase 3, Block D widens this assertion by exactly two
        paths — `GET`/`PUT /notification-preferences`, which Block B recorded
        as deferred to Block D. Nothing else moved: the inbox surface is
        unchanged, and there is still no delivery, worker or email endpoint."""
        paths = client.get("/openapi.json").json()["paths"]
        notification_paths = {
            path: sorted(methods) for path, methods in paths.items() if "notification" in path
        }

        assert notification_paths == {
            "/api/v1/notifications": ["get"],
            "/api/v1/notifications/unread-count": ["get"],
            "/api/v1/notifications/read-all": ["post"],
            "/api/v1/notifications/{recipient_id}/read": ["post"],
            "/api/v1/notification-preferences": ["get", "put"],
        }

    def test_there_is_no_notification_creation_endpoint(self, client):
        """Notifications originate from domain events through
        NotificationService — never from an API call (principle §4.1)."""
        paths = client.get("/openapi.json").json()["paths"]

        assert "post" not in paths.get("/api/v1/notifications", {})

    def test_the_delivery_lifecycle_is_not_exposed(self, client):
        """Block D adds the delivery state machine as an **internal** service.
        An endpoint over it would let a client drive a queue a worker owns —
        claiming rows, marking them sent — and there is no worker yet to
        arbitrate."""
        paths = client.get("/openapi.json").json()["paths"]

        assert not any("delivery" in path for path in paths)
        assert not any("notification-worker" in path for path in paths)
        assert not any(path.endswith("/resend") for path in paths)
