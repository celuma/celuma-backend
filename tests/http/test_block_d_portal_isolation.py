"""Physician-portal isolation and exposure boundary (Céluma 1.3, Phase 5, Block D — D7).

Closes the first half of C-005, which Block C carried as ACCEPTED-DEBT: the
portal's routes had been read and found "tenant- and publication-scoped", but
nothing executable proved it. This module is that proof — and it is also what
reproduced D-001, because the reading was wrong for one of the two routes.

`GET /portal/physician/orders` filters on `Order.tenant_id == ctx.tenant_id`.
`GET /portal/physician/orders/{id}/report` does not: it loads the order by its
path id and then gates only on `order.requested_by != user.email`. Since
`requested_by` is a free-text 255-char email string with no uniqueness across
tenants, two tenants may legitimately carry the same requesting physician's
address, and the second route hands one tenant's published report — with a
resolvable presigned PDF URL for patient data — to the other tenant's user.

The 404 (rather than 403) expected below is the convention this codebase
already uses for a path-addressed resource the caller does not own: every
other sample/order route hides existence, and C-001 fixed `laboratory.py` to
the same rule.
"""
from __future__ import annotations

import pytest
from sqlmodel import Session

from app.models.enums import ReportStatus

from .factories import (
    auth_headers,
    create_branch,
    create_order,
    create_patient,
    create_report,
    create_tenant,
    create_user,
)

PHYSICIAN_EMAIL = "dra.solicitante@example.com"


def _portal_tenant(
    session: Session,
    *,
    name: str,
    branch_code: str,
    order_code: str,
    patient_code: str,
    physician_email: str = PHYSICIAN_EMAIL,
    status=ReportStatus.PUBLISHED,
    requested_by: str | None = PHYSICIAN_EMAIL,
):
    """A self-contained tenant with a physician, a patient, an order carrying
    `requested_by`, and a report at `status` with a persisted PDF artifact."""
    tenant = create_tenant(session, name=name)
    branch = create_branch(session, tenant, code=branch_code)
    physician = create_user(
        session, tenant, email=physician_email, roles=("physician",)
    )
    patient = create_patient(session, tenant, branch, patient_code=patient_code)
    order = create_order(session, tenant, branch, order_code=order_code)
    order.patient_id = patient.id
    order.requested_by = requested_by
    session.add(order)
    session.commit()
    session.refresh(order)

    report, version = create_report(
        session,
        tenant,
        branch,
        order,
        status=status,
        pdf_generation_status="READY",
    )
    return {
        "tenant": tenant,
        "branch": branch,
        "physician": physician,
        "patient": patient,
        "order": order,
        "report": report,
        "version": version,
    }


class TestPortalAuthenticationRequired:
    """Neither physician route is reachable without a valid session.

    A **missing** `Authorization` header is a 403 here, not a 401: the portal
    uses the shared `HTTPBearer()` from `auth.py:42`, which runs with
    `auto_error=True`. Only the notifications router deliberately overrides
    that with its own `HTTPBearer(auto_error=False)` to return 401
    (`notifications.py:63-75`). This is pre-existing, deliberate and
    documented in that comment, so these tests assert the contract as it is
    rather than changing an authentication behaviour Block D has no finding
    against. A *present but invalid* token is a 401, asserted below.
    """

    def test_listing_orders_requires_authentication(self, client):
        assert client.get("/api/v1/portal/physician/orders").status_code == 403

    def test_reading_a_report_requires_authentication(self, client, session):
        a = _portal_tenant(
            session,
            name="Tenant A",
            branch_code="A",
            order_code="A-1",
            patient_code="PA-1",
        )
        response = client.get(
            f"/api/v1/portal/physician/orders/{a['order'].id}/report"
        )
        assert response.status_code == 403
        assert "pdf_url" not in response.text

    def test_an_invalid_token_is_rejected(self, client):
        response = client.get(
            "/api/v1/portal/physician/orders",
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert response.status_code == 401


class TestPortalPermissionRequired:
    """`portal:physician_access` is the gate; holding a clinical role is not
    the same thing as holding portal access."""

    def test_a_user_without_the_portal_permission_cannot_list(self, client, session):
        tenant = create_tenant(session, name="Tenant A")
        create_branch(session, tenant, code="A")
        pathologist = create_user(
            session, tenant, email="patologo@example.com", roles=("pathologist",)
        )
        response = client.get(
            "/api/v1/portal/physician/orders", headers=auth_headers(pathologist)
        )
        assert response.status_code == 403
        assert "portal:physician_access" in response.json()["detail"]

    def test_a_user_without_the_portal_permission_cannot_read_a_report(
        self, client, session
    ):
        a = _portal_tenant(
            session,
            name="Tenant A",
            branch_code="A",
            order_code="A-1",
            patient_code="PA-1",
        )
        pathologist = create_user(
            session, a["tenant"], email="patologo@example.com", roles=("pathologist",)
        )
        response = client.get(
            f"/api/v1/portal/physician/orders/{a['order'].id}/report",
            headers=auth_headers(pathologist),
        )
        assert response.status_code == 403


class TestPhysicianScoping:
    """A physician sees their own referrals and nobody else's."""

    def test_a_physician_sees_their_own_order(self, client, session):
        a = _portal_tenant(
            session,
            name="Tenant A",
            branch_code="A",
            order_code="A-1",
            patient_code="PA-1",
        )
        response = client.get(
            "/api/v1/portal/physician/orders", headers=auth_headers(a["physician"])
        )
        assert response.status_code == 200
        body = response.json()
        assert [row["order_code"] for row in body] == ["A-1"]

    def test_a_physician_does_not_see_another_physicians_order(
        self, client, session
    ):
        a = _portal_tenant(
            session,
            name="Tenant A",
            branch_code="A",
            order_code="A-1",
            patient_code="PA-1",
        )
        other = create_user(
            session, a["tenant"], email="otro.medico@example.com", roles=("physician",)
        )
        response = client.get(
            "/api/v1/portal/physician/orders", headers=auth_headers(other)
        )
        assert response.status_code == 200
        assert response.json() == []

    def test_a_physician_cannot_read_another_physicians_report(
        self, client, session
    ):
        a = _portal_tenant(
            session,
            name="Tenant A",
            branch_code="A",
            order_code="A-1",
            patient_code="PA-1",
        )
        other = create_user(
            session, a["tenant"], email="otro.medico@example.com", roles=("physician",)
        )
        response = client.get(
            f"/api/v1/portal/physician/orders/{a['order'].id}/report",
            headers=auth_headers(other),
        )
        assert response.status_code == 403


class TestPortalTenantIsolation:
    """D-001. Two tenants that share a requesting physician's email address —
    a routine situation, since `requested_by` is free text and a physician may
    refer to more than one laboratory."""

    @pytest.fixture
    def two_tenants(self, session):
        a = _portal_tenant(
            session,
            name="Tenant A",
            branch_code="A",
            order_code="A-1",
            patient_code="PA-1",
        )
        b = _portal_tenant(
            session,
            name="Tenant B",
            branch_code="B",
            order_code="B-1",
            patient_code="PB-1",
        )
        return a, b

    def test_the_listing_is_tenant_scoped(self, client, session, two_tenants):
        a, b = two_tenants
        response = client.get(
            "/api/v1/portal/physician/orders", headers=auth_headers(a["physician"])
        )
        assert response.status_code == 200
        codes = [row["order_code"] for row in response.json()]
        assert codes == ["A-1"], "tenant A's physician must not see tenant B's order"

    def test_another_tenants_report_cannot_be_read_by_order_id(
        self, client, session, two_tenants
    ):
        """The core D-001 case: same email, different tenant, direct id."""
        a, b = two_tenants
        response = client.get(
            f"/api/v1/portal/physician/orders/{b['order'].id}/report",
            headers=auth_headers(a["physician"]),
        )
        assert response.status_code == 404, (
            "tenant A's physician read tenant B's published report through the "
            "portal; the route never anchors the order to the caller's tenant"
        )

    def test_another_tenants_report_does_not_leak_a_pdf_url(
        self, client, session, two_tenants
    ):
        a, b = two_tenants
        response = client.get(
            f"/api/v1/portal/physician/orders/{b['order'].id}/report",
            headers=auth_headers(a["physician"]),
        )
        assert "pdf_url" not in response.text
        assert str(b["report"].id) not in response.text

    def test_the_reverse_direction_is_also_blocked(
        self, client, session, two_tenants
    ):
        a, b = two_tenants
        response = client.get(
            f"/api/v1/portal/physician/orders/{a['order'].id}/report",
            headers=auth_headers(b["physician"]),
        )
        assert response.status_code == 404


class TestPublicationBoundary:
    """Only a PUBLISHED report is exposed. Every other lifecycle state — and a
    retraction — is refused, which is the portal's existing contract."""

    @pytest.mark.parametrize(
        "status",
        [
            ReportStatus.DRAFT,
            ReportStatus.IN_REVIEW,
            ReportStatus.APPROVED,
            ReportStatus.RETRACTED,
        ],
    )
    def test_an_unpublished_report_is_not_exposed(self, client, session, status):
        a = _portal_tenant(
            session,
            name="Tenant A",
            branch_code="A",
            order_code="A-1",
            patient_code="PA-1",
            status=status,
        )
        response = client.get(
            f"/api/v1/portal/physician/orders/{a['order'].id}/report",
            headers=auth_headers(a["physician"]),
        )
        assert response.status_code == 403
        assert "pdf_url" not in response.text

    def test_a_published_report_is_exposed(self, client, session):
        a = _portal_tenant(
            session,
            name="Tenant A",
            branch_code="A",
            order_code="A-1",
            patient_code="PA-1",
        )
        response = client.get(
            f"/api/v1/portal/physician/orders/{a['order'].id}/report",
            headers=auth_headers(a["physician"]),
        )
        assert response.status_code == 200
        assert response.json()["status"] == ReportStatus.PUBLISHED.value

    def test_a_billed_lock_blocks_access(self, client, session):
        a = _portal_tenant(
            session,
            name="Tenant A",
            branch_code="A",
            order_code="A-1",
            patient_code="PA-1",
        )
        a["order"].billed_lock = True
        session.add(a["order"])
        session.commit()
        response = client.get(
            f"/api/v1/portal/physician/orders/{a['order'].id}/report",
            headers=auth_headers(a["physician"]),
        )
        assert response.status_code == 403
        assert "pdf_url" not in response.text


class TestPortalPayloadBoundary:
    """The portal is a patient/physician-facing surface. Its payload carries
    what a referring physician needs and none of Céluma's internal report
    machinery."""

    #: Fields that belong to the internal review/versioning/audit model and
    #: must not cross the portal boundary.
    FORBIDDEN_KEYS = {
        "version_no",
        "versions",
        "version_history",
        "audit",
        "audit_events",
        "review",
        "reviews",
        "review_status",
        "reviewed_by",
        "approved_by",
        "authored_by",
        "created_by",
        "template_version_id",
        "json_storage_id",
        "pdf_storage_id",
        "pdf_sha256",
        "internal_notes",
        "object_key",
        "tenant_id",
    }

    def test_the_report_payload_exposes_only_the_portal_contract(
        self, client, session
    ):
        a = _portal_tenant(
            session,
            name="Tenant A",
            branch_code="A",
            order_code="A-1",
            patient_code="PA-1",
        )
        response = client.get(
            f"/api/v1/portal/physician/orders/{a['order'].id}/report",
            headers=auth_headers(a["physician"]),
        )
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {
            "report_id",
            "order_code",
            "status",
            "title",
            "published_at",
            "pdf_url",
        }
        assert not (set(body) & self.FORBIDDEN_KEYS)

    def test_the_listing_payload_exposes_only_the_portal_contract(
        self, client, session
    ):
        a = _portal_tenant(
            session,
            name="Tenant A",
            branch_code="A",
            order_code="A-1",
            patient_code="PA-1",
        )
        response = client.get(
            "/api/v1/portal/physician/orders", headers=auth_headers(a["physician"])
        )
        assert response.status_code == 200
        row = response.json()[0]
        assert set(row) == {
            "id",
            "order_code",
            "patient_name",
            "patient_code",
            "status",
            "has_report",
            "report_status",
            "requested_by",
        }
        assert not (set(row) & self.FORBIDDEN_KEYS)


class TestIdentifierProbing:
    """An identifier is not an authorization decision."""

    def test_an_unknown_order_id_is_not_found(self, client, session):
        a = _portal_tenant(
            session,
            name="Tenant A",
            branch_code="A",
            order_code="A-1",
            patient_code="PA-1",
        )
        import uuid as _uuid

        response = client.get(
            f"/api/v1/portal/physician/orders/{_uuid.uuid4()}/report",
            headers=auth_headers(a["physician"]),
        )
        assert response.status_code == 404

    def test_an_order_with_no_requesting_physician_is_not_exposed(
        self, client, session
    ):
        a = _portal_tenant(
            session,
            name="Tenant A",
            branch_code="A",
            order_code="A-1",
            patient_code="PA-1",
            requested_by=None,
        )
        response = client.get(
            f"/api/v1/portal/physician/orders/{a['order'].id}/report",
            headers=auth_headers(a["physician"]),
        )
        assert response.status_code in (403, 404)
        assert "pdf_url" not in response.text
