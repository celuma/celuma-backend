"""Plain (non-fixture) factory helpers for HTTP integration tests.

Kept intentionally simple — direct SQLModel inserts, not a full factory
framework — per Céluma1.3-Fase2.md §11 ("No construyas un framework de
testing innecesariamente complejo").
"""
import uuid
from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlmodel import Session

from app.core.rbac import assign_role_by_code
from app.core.security import create_jwt, hash_password
from app.models.laboratory import Order
from app.models.report import Report, ReportVersion
from app.models.report_template_version import ReportTemplateVersion, ReportTemplateVersionStatus
from app.models.storage import StorageObject
from app.models.tenant import Branch, Tenant
from app.models.user import AppUser


# Céluma 1.3 Fase 2, Bloque B, Historia B10: the RBAC catalog (permissions,
# system roles, role<->permission links) is real seed data applied by
# alembic/versions/v1_0_0_initial_schema.py — the HTTP test database is
# built by running the actual migration chain (see conftest.py), so it is
# already populated. Tests assign real system role codes via
# `assign_role_by_code`, exactly like production registration does; there
# is no separate/duplicated test-only RBAC fixture.
#
# "superuser" holds every permission (_ALL_PERMISSION_CODES in that
# migration) — used as the default "can do everything" test role.
# "reviewer" (added in v1_1_0) holds reports:sign + reports:read, matching
# ROLE_REVIEWER in app/core/rbac.py.


def create_tenant(session: Session, *, name: str = "Test Tenant", reports_v2_enabled: bool = False) -> Tenant:
    tenant = Tenant(name=name, reports_v2_enabled=reports_v2_enabled)
    session.add(tenant)
    session.commit()
    session.refresh(tenant)
    return tenant


def create_branch(session: Session, tenant: Tenant, *, code: str = "MAIN") -> Branch:
    branch = Branch(tenant_id=tenant.id, code=code, name=f"Branch {code}")
    session.add(branch)
    session.commit()
    session.refresh(branch)
    return branch


def create_user(
    session: Session,
    tenant: Tenant,
    *,
    email: str,
    roles: Iterable[str] = ("superuser",),
) -> AppUser:
    user = AppUser(
        tenant_id=tenant.id,
        email=email,
        username=email.split("@")[0],
        full_name="Test User",
        first_name="Test",
        last_name="User",
        hashed_password=hash_password("irrelevant-in-tests"),
    )
    session.add(user)
    session.flush()
    for role_code in roles:
        assign_role_by_code(user.id, role_code, session)
    session.commit()
    session.refresh(user)
    return user


def auth_headers(user: AppUser) -> dict[str, str]:
    token = create_jwt(sub=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def create_order(session: Session, tenant: Tenant, branch: Branch, *, order_code: str = "ORD-1") -> Order:
    order = Order(tenant_id=tenant.id, branch_id=branch.id, order_code=order_code)
    session.add(order)
    session.commit()
    session.refresh(order)
    return order


def create_storage_object(session: Session, *, key: str = "logos/test.png") -> StorageObject:
    obj = StorageObject(
        provider="aws",
        region="mx-test-1",
        bucket="celuma-test-bucket",
        object_key=key,
        content_type="image/png",
        size_bytes=1234,
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


def valid_rendering_snapshot(**overrides) -> dict:
    """A ReportRenderingSnapshotV2-shaped payload usable as the `configuration`
    of a `ReportTemplateVersionCreate` request."""
    base = {
        "schema_version": 2,
        "template": {
            "base": {"diagnosis": {"label": "Diagnóstico", "type": "text"}},
            "sections": {},
            "base_order": ["diagnosis"],
            "section_order": [],
        },
        "presentation": {
            "paper": {
                "size": "LETTER",
                "orientation": "PORTRAIT",
                "margins_cm": {"top": 2.0, "right": 2.0, "bottom": 2.0, "left": 2.0},
            },
            "header": {
                "enabled": True,
                "institution_name": "Céluma Labs",
                "subtitle": "Diagnóstico Anatomopatológico",
                "address": "Av. Siempre Viva 123",
                "phone": "+52 55 1234 5678",
                "email": "contacto@celuma.example",
            },
            "footer": {"enabled": True, "custom_text": "Confidencial", "show_page_number": True},
            "style": {"primary_color": "#336699"},
        },
    }
    base.update(overrides)
    return base


def create_published_v2_report_directly(
    session: Session,
    tenant: Tenant,
    branch: Branch,
    order: Order,
    template_version: ReportTemplateVersion,
    *,
    status,
) -> tuple[Report, ReportVersion]:
    """Insert a Report + ReportVersion directly at a given status, bypassing
    the submit/approve/sign HTTP lifecycle (which lives outside reports.py's
    template-version scope). Used only to test status-guard endpoints
    (Historia B9), not to exercise the review workflow itself.
    """
    report = Report(
        tenant_id=tenant.id,
        branch_id=branch.id,
        order_id=order.id,
        status=status,
    )
    session.add(report)
    session.flush()
    version = ReportVersion(
        report_id=report.id,
        version_no=1,
        is_current=True,
        schema_version=2,
        template_version_id=template_version.id,
    )
    session.add(version)
    session.commit()
    session.refresh(report)
    session.refresh(version)
    return report, version
