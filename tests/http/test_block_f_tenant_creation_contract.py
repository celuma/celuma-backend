"""Tenant-creation contract after E-012 (Céluma 1.3, Phase 5, Block F §1).

Block E's closure verification found `POST /api/v1/tenants/` authenticated by
the router-level `current_user` but gated by nothing else: the handler declared
neither `ctx` nor `user` and called no permission helper, so the least
privileged role in the system could persist a `Tenant` row **and** a
`TenantUsage` row carrying no branch and no user — an orphan tenant nobody can
authenticate into, permanently occupying the accounting substrate that billing
and reconciliation are computed from.

No gate in the 33-code catalogue fits. `admin:manage_tenant` is seeded as
administration of the caller's *own* laboratory and `superuser` as *"Acceso
total al tenant"* — both tenant-scoped — and `AppUser.tenant_id` is a
non-nullable FK, so 1.3 has no platform-level user for a platform-level
permission to attach to. Inventing one is a product decision, not a security
patch.

The route was therefore removed rather than gated. It had no frontend caller
and no test, and tenant onboarding already has a real, supported path:
`POST /api/v1/auth/register/unified`, which creates tenant + default branch +
admin user atomically and produces a tenant that can actually be logged into.

`GET /api/v1/tenants/` still occupies the same path, so the collection now
answers **405** to a POST — the absence is the assertion. The `TenantCreate`
schema is deliberately left in `app/schemas/tenant.py`; it is covered by
`tests/test_schemas.py` and removing it is not part of this contract change.
"""
from __future__ import annotations

from sqlmodel import Session, func, select

from app.models.tenant import Tenant
from app.models.tenant_usage import TenantUsage

from .factories import auth_headers, create_branch, create_tenant, create_user


def _counts(session: Session) -> tuple[int, int]:
    """(tenants, tenant_usage rows) — the two tables the route used to write."""
    session.expire_all()
    return (
        session.exec(select(func.count()).select_from(Tenant)).one(),
        session.exec(select(func.count()).select_from(TenantUsage)).one(),
    )


def _payload() -> dict:
    """The exact body the removed route accepted."""
    return {
        "name": "Orphan Lab",
        "legal_name": "Orphan Lab S.A. de C.V.",
        "tax_id": "OLA010101AAA",
    }


def _member(session: Session, *, roles: tuple[str, ...], email: str):
    tenant = create_tenant(session, name="Lab F")
    create_branch(session, tenant, code="F-MAIN")
    return create_user(session, tenant, email=email, roles=roles)


# ---------------------------------------------------------------------------
# E-012 — the route is gone, for every role that could previously reach it
# ---------------------------------------------------------------------------

def test_post_tenants_collection_is_not_routed_for_viewer(client, session):
    """The least-privileged role: 200 Created before the removal, 405 now."""
    user = _member(session, roles=("viewer",), email="viewer.f@example.com")
    before = _counts(session)

    response = client.post(
        "/api/v1/tenants/", json=_payload(), headers=auth_headers(user),
    )

    assert response.status_code == 405, response.text
    assert _counts(session) == before


def test_post_tenants_collection_is_not_routed_for_admin(client, session):
    """Removal is unconditional — holding `admin:manage_tenant` does not
    resurrect the route. That permission administers the caller's own
    laboratory; it was never a platform-provisioning grant."""
    user = _member(session, roles=("admin",), email="admin.f@example.com")
    before = _counts(session)

    response = client.post(
        "/api/v1/tenants/", json=_payload(), headers=auth_headers(user),
    )

    assert response.status_code == 405, response.text
    assert _counts(session) == before


def test_post_tenants_collection_is_not_routed_for_superuser(client, session):
    """`superuser` is seeded as "Acceso total **al tenant**" — tenant-scoped,
    so it is not the platform grant this route would have needed either."""
    user = _member(session, roles=("superuser",), email="super.f@example.com")
    before = _counts(session)

    response = client.post(
        "/api/v1/tenants/", json=_payload(), headers=auth_headers(user),
    )

    assert response.status_code == 405, response.text
    assert _counts(session) == before


def test_post_tenants_collection_creates_no_orphan_usage_row(client, session):
    """The write that mattered: the removed handler called
    `UsageService.initialize_usage(..., source="tenant_creation")` in the same
    transaction as the insert. `TenantUsage` is keyed by `tenant_id` and keeps
    no provenance column, so the assertion is structural — no tenant may exist
    with neither branch nor user, and no usage row may point at one."""
    user = _member(session, roles=("viewer",), email="usage.f@example.com")
    _, usage_before = _counts(session)

    client.post("/api/v1/tenants/", json=_payload(), headers=auth_headers(user))

    session.expire_all()
    orphans = [
        t for t in session.exec(select(Tenant)).all()
        if not t.branches and not t.users
    ]
    assert orphans == []
    assert session.exec(select(func.count()).select_from(TenantUsage)).one() == usage_before


# ---------------------------------------------------------------------------
# Positive controls — the removal is scoped to the one verb on the one path
# ---------------------------------------------------------------------------

def test_get_tenants_collection_still_works(client, session):
    """`GET /api/v1/tenants/` shares the path and is unaffected."""
    user = _member(session, roles=("viewer",), email="list.f@example.com")

    response = client.get("/api/v1/tenants/", headers=auth_headers(user))

    assert response.status_code == 200, response.text
    assert [t["id"] for t in response.json()] == [str(user.tenant_id)]


def test_unauthenticated_post_to_tenants_collection_still_refused(client, session):
    """No credential, no route — and still no side effect."""
    before = _counts(session)

    response = client.post("/api/v1/tenants/", json=_payload())

    assert response.status_code in (401, 403, 405), response.text
    assert _counts(session) == before
