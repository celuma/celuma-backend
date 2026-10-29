"""Who may clean a laboratory (Céluma 1.4, issue #39; ADR 0021, decision E1 — owner decision of 2026-10-28).

The laboratory-content cleanup is a protected capability held by ONE person per laboratory: **the person who
created the laboratory during its initial registration** (the "superadministrator"). It is not a role and not a
permission anyone can be granted:

* `tenant.created_by_user_id` is set atomically by `POST /auth/register/unified` and is WRITE-ONCE at the
  database (trigger `trg_tenant_creator_write_once`): no role/custom-role/user/invitation/tenant-update API, and
  no SQL client, can set, change or clear it once it holds a value;
* the capability is evaluated from that column alone — never from `superuser`, `admin` or any other role (the
  `superuser` role is a catalog role that tenant administrators can grant themselves, so it proves nothing);
* it is own-tenant only: the path tenant must be the caller's tenant (another tenant answers 404, revealing
  nothing) and the caller must be an ACTIVE user bound to that tenant;
* a laboratory with no recorded creator (every laboratory that predates the binding: there is no reliable
  provenance for it) FAILS CLOSED until a controlled, explicit one-time backfill — see the runbook.

No platform-wide or cross-laboratory authority exists or is introduced.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException
from sqlmodel import Session

from app.models.tenant import Tenant
from app.models.user import AppUser

logger = logging.getLogger(__name__)

NOT_AUTHORIZED = "laboratory_cleanup_not_authorized"


def _normalize(value) -> str | None:
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


@dataclass(frozen=True)
class CleanupOperator:
    user: AppUser
    tenant: Tenant


def is_authorized(session: Session, user: AppUser, tenant_id) -> bool:
    """The conditions above, as a boolean (also what the UI capability probe reports)."""
    target = _normalize(tenant_id)
    if target is None or str(user.tenant_id) != target or not user.is_active:
        return False
    tenant = session.get(Tenant, user.tenant_id)
    return tenant is not None and tenant.created_by_user_id is not None and tenant.created_by_user_id == user.id


def require_operator(session: Session, user: AppUser, tenant_id) -> CleanupOperator:
    """Backend enforcement for every cleanup endpoint.

    A path naming another tenant is a 404 (indistinguishable from a tenant that does not exist); anything else
    that fails is a 403 with one stable code and no hint about which condition."""
    target = _normalize(tenant_id)
    if target is None or str(user.tenant_id) != target:
        raise HTTPException(404, "Laboratory not found")
    if not is_authorized(session, user, target):
        raise HTTPException(403, {
            "code": NOT_AUTHORIZED,
            "message": "Solo la persona que creó este laboratorio puede limpiar su contenido.",
        })
    tenant = session.get(Tenant, user.tenant_id)
    if tenant is None:
        raise HTTPException(404, "Laboratory not found")
    return CleanupOperator(user=user, tenant=tenant)
