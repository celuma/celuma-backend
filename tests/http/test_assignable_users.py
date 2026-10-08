"""Céluma 1.3.2: the assignee picker must not inherit the ten-mention cap."""
from uuid import UUID

import pytest
from sqlmodel import select

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
            username=f"usuario{index}", email=f"user{index}@example.invalid",
            hashed_password="unused-synthetic-fixture",
        )
        session.add(candidate)
        session.flush()
        expected.add(str(candidate.id))
    inactive = AppUser(tenant_id=tenant.id, full_name="Inactivo", email="inactive@example.invalid",
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


def test_cursor_survives_deactivation_and_does_not_accept_foreign_users(client, session):
    tenant = create_tenant(session)
    actor = create_user(session, tenant, email="reader@example.invalid", roles=("viewer",))
    for index in range(12):
        session.add(AppUser(tenant_id=tenant.id, full_name="Same name",
                            email=f"user{index}@example.invalid", hashed_password="unused"))
    session.commit()
    first = client.get(URL, params={"limit": 5}, headers=auth_headers(actor)).json()
    cursor = first["next_after"]
    cursor_user = session.get(AppUser, UUID(cursor))
    cursor_user.is_active = False
    session.add(cursor_user)
    session.commit()
    following = client.get(URL, params={"after": cursor}, headers=auth_headers(actor))
    assert following.status_code == 200
    actual = {u["id"] for u in following.json()["users"]}
    expected = {str(u.id) for u in session.exec(select(AppUser).where(
        AppUser.tenant_id == tenant.id, AppUser.is_active == True, AppUser.id > UUID(cursor)
    )).all()}
    assert actual == expected


def test_requires_lab_read_permission(client, session):
    tenant = create_tenant(session)
    actor = create_user(session, tenant, email="physician@example.invalid", roles=("physician",))
    assert client.get(URL, headers=auth_headers(actor)).status_code == 403
    assert client.get(URL).status_code == 401


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
