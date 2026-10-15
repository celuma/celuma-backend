"""Céluma 1.3.2: the assignee picker must not inherit the ten-mention cap."""
from uuid import UUID

import pytest

from app.models.user import AppUser
from tests.http.factories import auth_headers, create_tenant, create_user

URL = "/api/v1/laboratory/users/assignable"


def test_pages_include_all_active_tenant_users(client, session):
    tenant = create_tenant(session)
    actor = create_user(session, tenant, email="reader@example.invalid", roles=("viewer",))
    expected = {str(actor.id)}
    for index in range(105):
        candidate = AppUser(
            tenant_id=tenant.id, full_name=f"Usuario {index:03}",
            first_name="Usuario", last_name=str(index),
            username=f"usuario{index}", email=f"user{index}@example.invalid",
            hashed_password="unused-synthetic-fixture",
        )
        session.add(candidate)
        session.flush()
        expected.add(str(candidate.id))
    inactive = AppUser(tenant_id=tenant.id, full_name="Inactivo", email="inactive@example.invalid",
                       first_name="Usuario", last_name="Inactivo",
                       is_active=False, hashed_password="unused-synthetic-fixture")
    session.add(inactive)
    other = create_tenant(session, name="Other synthetic lab")
    create_user(session, other, email="other@example.invalid")
    session.commit()

    collected = []
    after = None
    pages = 0
    while True:
        params = {"limit": 25}
        if after:
            params["after"] = after
        response = client.get(URL, params=params, headers=auth_headers(actor))
        assert response.status_code == 200
        body = response.json()
        assert len(body["users"]) <= 25
        collected.extend(body["users"])
        pages += 1
        after = body["next_after"]
        if after is None:
            break
        assert pages < 10
    ids = [u["id"] for u in collected]
    assert set(ids) == expected
    assert len(ids) == len(set(ids)) == 106
    assert ids == sorted(ids)
    assert all(set(u) == {"id", "name", "email", "username", "avatar_url"} for u in collected)

    # The mention autocompleter keeps its bounded, backward-compatible contract.
    mentions = client.get("/api/v1/laboratory/users/search", headers=auth_headers(actor))
    assert mentions.status_code == 200
    assert len(mentions.json()["users"]) == 10


def _ids(response):
    return [u["id"] for u in response.json()["users"]]


def test_cursor_survives_deactivation(client, session):
    tenant = create_tenant(session)
    actor = create_user(session, tenant, email="reader@example.invalid", roles=("viewer",))
    # Fixed low ids sort ahead of every random uuid4 id, so the actor is always
    # last in the keyset order and can never be the user a cursor points at.
    # (Deactivating the authenticated actor is a different, correct 401.)
    assert actor.id > UUID(int=100)
    for position in range(1, 13):
        session.add(AppUser(
            id=UUID(int=position), tenant_id=tenant.id, full_name="Same name",
            first_name="Usuario", last_name=str(position),
            email=f"user{position}@example.invalid", hashed_password="unused",
            is_active=position != 8,  # already inactive, sits inside the second page
        ))
    foreign = create_tenant(session, name="Other synthetic lab")
    session.add(AppUser(
        id=UUID(int=100), tenant_id=foreign.id, full_name="Foreign",
        first_name="Usuario", last_name="Foreign",
        email="foreign@example.invalid", hashed_password="unused",
    ))
    session.commit()

    first = client.get(URL, params={"limit": 5}, headers=auth_headers(actor))
    assert first.status_code == 200
    assert _ids(first) == [str(UUID(int=n)) for n in range(1, 6)]
    cursor = first.json()["next_after"]
    assert cursor == str(UUID(int=5))
    assert cursor != str(actor.id)

    cursor_user = session.get(AppUser, UUID(cursor))
    cursor_user.is_active = False
    session.add(cursor_user)
    session.commit()

    # Continue from the now-inactive cursor: skips the inactive user 8 and
    # resumes strictly after the cursor, with a fresh cursor and no repeats.
    second = client.get(URL, params={"after": cursor, "limit": 3}, headers=auth_headers(actor))
    assert second.status_code == 200
    assert _ids(second) == [str(UUID(int=n)) for n in (6, 7, 9)]
    assert second.json()["next_after"] == str(UUID(int=9))

    # Last page: the foreign-tenant user (id 100) sorts between 12 and the
    # actor and must not leak; the actor itself is still listed.
    third = client.get(URL, params={"after": str(UUID(int=9))}, headers=auth_headers(actor))
    assert third.status_code == 200
    assert _ids(third) == [str(UUID(int=n)) for n in (10, 11, 12)] + [str(actor.id)]
    assert third.json()["next_after"] is None


def test_deactivated_actor_is_rejected(client, session):
    tenant = create_tenant(session)
    actor = create_user(session, tenant, email="reader@example.invalid", roles=("viewer",))
    headers = auth_headers(actor)
    assert client.get(URL, headers=headers).status_code == 200
    actor.is_active = False
    session.add(actor)
    session.commit()
    response = client.get(URL, headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"] == "Inactive user"


def test_requires_lab_read_permission(client, session):
    tenant = create_tenant(session)
    actor = create_user(session, tenant, email="physician@example.invalid", roles=("physician",))
    assert client.get(URL, headers=auth_headers(actor)).status_code == 403
    assert client.get(URL).status_code == 403


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"after": "invalid"}])
def test_validates_pagination(client, session, params):
    tenant = create_tenant(session)
    actor = create_user(session, tenant, email="reader@example.invalid", roles=("viewer",))
    assert client.get(URL, params=params, headers=auth_headers(actor)).status_code == 422


def test_empty_page_has_no_cursor(client, session):
    tenant = create_tenant(session)
    actor = create_user(session, tenant, email="reader@example.invalid", roles=("viewer",))
    response = client.get(URL, params={"after": "ffffffff-ffff-ffff-ffff-ffffffffffff"},
                          headers=auth_headers(actor))
    assert response.status_code == 200
    assert response.json() == {"users": [], "next_after": None}
