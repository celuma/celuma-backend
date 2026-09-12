"""CEL-131-11 — the insecure legacy registration route is gone.

`POST /api/v1/auth/register` was unauthenticated and took both `tenant_id` and
`role` straight from the request body, then handed whatever arrived to
`assign_role_by_code`. Anyone who knew or guessed a tenant UUID could create
themselves an account in that laboratory with any role in the catalogue:

  * `reviewer`   — defeats the clinical boundary the rest of Block A builds;
  * `admin`      — full operational control of the laboratory;
  * `superuser`  — the entire permission catalogue: total compromise.

The route was REMOVED rather than gated, following the precedent set for the
equally unsafe `POST /api/v1/tenants/` (test_block_f_tenant_creation_contract.py):
deciding who may self-register, into which tenant, with which role, is a
product decision about onboarding, not something to invent inside a security
hotfix. It had no frontend caller and no test; real onboarding goes through
`POST /api/v1/auth/register/unified`.

As in that precedent, **the absence is the assertion.** These tests fail the
moment anything re-routes that path, whatever it does behind it.
"""
from __future__ import annotations

from sqlmodel import Session, func, select

from app.models.user import AppUser

from .factories import create_branch, create_tenant


def _user_count(session: Session) -> int:
    session.expire_all()
    return session.exec(select(func.count()).select_from(AppUser)).one()


def _payload(tenant_id: str, role: str) -> dict:
    """The exact body the removed route accepted."""
    return {
        "email": f"attacker+{role}@evil.example",
        "username": f"attacker_{role}",
        "full_name": "Unauthorized Person",
        "password": "hunter2-hunter2",
        "tenant_id": tenant_id,
        "role": role,
    }


def _tenant(session: Session):
    tenant = create_tenant(session, name="Laboratorio Objetivo")
    create_branch(session, tenant, code="MAIN")
    return tenant


# ---------------------------------------------------------------------------
# The route is gone for every role it could previously mint
# ---------------------------------------------------------------------------

def test_an_unauthenticated_caller_cannot_create_a_reviewer(client, session):
    """The role this hotfix exists to protect. Before the removal this
    returned 200 and produced a user holding `reports:approve` and
    `reports:sign` — the whole clinical authorization boundary, self-issued."""
    tenant = _tenant(session)
    before = _user_count(session)

    response = client.post(
        "/api/v1/auth/register", json=_payload(str(tenant.id), "reviewer")
    )

    assert response.status_code == 404, response.text
    assert _user_count(session) == before


def test_an_unauthenticated_caller_cannot_create_an_admin(client, session):
    tenant = _tenant(session)
    before = _user_count(session)

    response = client.post(
        "/api/v1/auth/register", json=_payload(str(tenant.id), "admin")
    )

    assert response.status_code == 404, response.text
    assert _user_count(session) == before


def test_an_unauthenticated_caller_cannot_create_a_superuser(client, session):
    """The worst case: `superuser` holds the entire permission catalogue, so
    this was total compromise of the target laboratory from an unauthenticated
    request."""
    tenant = _tenant(session)
    before = _user_count(session)

    response = client.post(
        "/api/v1/auth/register", json=_payload(str(tenant.id), "superuser")
    )

    assert response.status_code == 404, response.text
    assert _user_count(session) == before


def test_an_unauthenticated_caller_cannot_create_a_user_in_an_arbitrary_tenant(
    client, session
):
    """Tenant isolation was never enforced on this route — `tenant_id` came
    from the request body and was used verbatim. Asserted against a real,
    populated tenant so a pass cannot come from the tenant simply not
    existing."""
    victim = _tenant(session)
    before = _user_count(session)

    response = client.post(
        "/api/v1/auth/register", json=_payload(str(victim.id), "pathologist")
    )

    assert response.status_code == 404, response.text
    assert _user_count(session) == before

    # And nothing was attached to the victim tenant.
    session.expire_all()
    assert (
        session.exec(
            select(func.count())
            .select_from(AppUser)
            .where(AppUser.tenant_id == victim.id)
        ).one()
        == 0
    )


def test_the_route_is_gone_for_every_seeded_role(client, session):
    """Exhaustive rather than illustrative: no role in the catalogue is
    reachable, including the ones that look harmless. `viewer` still reads
    every patient and report in the laboratory."""
    tenant = _tenant(session)
    before = _user_count(session)

    for role in (
        "superuser",
        "admin",
        "pathologist",
        "reviewer",
        "lab_tech",
        "assistant",
        "billing",
        "viewer",
        "physician",
    ):
        response = client.post(
            "/api/v1/auth/register", json=_payload(str(tenant.id), role)
        )
        assert response.status_code == 404, f"{role}: {response.text}"

    assert _user_count(session) == before


def test_a_malformed_body_is_equally_unrouted(client, session):
    """404 is the path being absent, not validation rejecting the body — so it
    must not depend on the body at all. If this ever returns 422, the route is
    back and something is parsing input again."""
    response = client.post("/api/v1/auth/register", json={})
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# The supported paths are untouched
# ---------------------------------------------------------------------------

def test_the_unified_registration_path_still_exists(client, session):
    """The removal must not take real onboarding with it. Asserted as "not
    404": the happy path of `/register/unified` has its own tests, and what
    matters here is only that the route is still routed."""
    response = client.post("/api/v1/auth/register/unified", json={})
    assert response.status_code != 404, response.text
    # A routed endpoint rejecting an empty body — i.e. it is parsing input,
    # which is exactly what the removed route must no longer do.
    assert response.status_code == 422, response.text


def test_login_is_untouched(client, session):
    response = client.post("/api/v1/auth/login", json={})
    assert response.status_code == 422, response.text
