"""Laboratory cleanup authority: an active Superadministrator of the same tenant.

Owner clarification 2026-10-29: use the existing system `superuser` role, assigned
automatically at initial registration. Historical creator identity is not an
authorization condition. Normal role/user/invitation APIs cannot grant or remove
this reserved role. Every cleanup endpoint checks live membership server-side.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException
from sqlmodel import Session

from app.core.rbac import ROLE_SUPERUSER, has_any_role
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
    return tenant is not None and has_any_role(user.id, {ROLE_SUPERUSER}, session)


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
            "message": "Solo un superadministrador activo de este laboratorio puede limpiar su contenido.",
        })
    tenant = session.get(Tenant, user.tenant_id)
    if tenant is None:
        raise HTTPException(404, "Laboratory not found")
    return CleanupOperator(user=user, tenant=tenant)
