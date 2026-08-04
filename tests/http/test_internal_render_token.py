"""HTTP integration tests for the internal render-data endpoint and its
render-token authorization (Céluma 1.3, Phase 2, Block E, Story E3).

This endpoint is deliberately NOT protected by `current_user` — a headless
browser rendering one report_version_id has no user session — so these
tests call it directly with an `Authorization: Bearer <render_token>`
header, never `auth_headers(user)`.
"""
from app.core.security import create_render_token
from app.models.enums import ReportStatus
from app.models.report import Report, ReportVersion

from .factories import create_branch, create_order, create_tenant, create_user


def _create_report(session, tenant, branch, order):
    report = Report(tenant_id=tenant.id, branch_id=branch.id, order_id=order.id, status=ReportStatus.DRAFT)
    session.add(report)
    session.flush()
    version = ReportVersion(report_id=report.id, version_no=1, is_current=True)
    session.add(version)
    session.commit()
    session.refresh(report)
    session.refresh(version)
    return report, version


def _render_data_url(report_id, version_no=1) -> str:
    return f"/api/v1/reports/internal/render-data/{report_id}/{version_no}"


class TestRenderTokenAuthorization:
    def test_missing_token_is_rejected(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        report, _ = _create_report(session, tenant, branch, order)

        resp = client.get(_render_data_url(report.id))
        assert resp.status_code in (401, 403)

    def test_malformed_token_is_rejected(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        report, _ = _create_report(session, tenant, branch, order)

        resp = client.get(
            _render_data_url(report.id), headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert resp.status_code == 401

    def test_a_normal_user_session_jwt_is_not_a_valid_render_token(self, client, session):
        """Defense in depth: a real user JWT (different secret/payload shape,
        `type` claim absent) must not be accepted here."""
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="u@t1.example")
        report, _ = _create_report(session, tenant, branch, order)

        from app.core.security import create_jwt

        user_token = create_jwt(sub=str(user.id))
        resp = client.get(
            _render_data_url(report.id), headers={"Authorization": f"Bearer {user_token}"}
        )
        assert resp.status_code == 401

    def test_token_for_a_different_version_is_rejected(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        report, version = _create_report(session, tenant, branch, order)

        # Mint a token for a version_id that doesn't correspond to the one
        # being requested.
        token = create_render_token("00000000-0000-0000-0000-000000000000", str(tenant.id), 90)
        resp = client.get(
            _render_data_url(report.id), headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 403

    def test_token_for_a_different_tenant_is_rejected(self, client, session):
        tenant_a = create_tenant(session, name="Tenant A")
        branch_a = create_branch(session, tenant_a)
        order_a = create_order(session, tenant_a, branch_a)
        report, version = _create_report(session, tenant_a, branch_a, order_a)

        tenant_b = create_tenant(session, name="Tenant B")
        token = create_render_token(str(version.id), str(tenant_b.id), 90)

        resp = client.get(
            _render_data_url(report.id), headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 404

    def test_valid_token_returns_the_render_envelope(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        report, version = _create_report(session, tenant, branch, order)

        token = create_render_token(str(version.id), str(tenant.id), 90)
        resp = client.get(
            _render_data_url(report.id), headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == str(report.id)
        assert body["version_no"] == 1
        assert "signer_lookup" in body

    def test_expired_token_is_rejected(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        report, version = _create_report(session, tenant, branch, order)

        token = create_render_token(str(version.id), str(tenant.id), -1)  # already expired
        resp = client.get(
            _render_data_url(report.id), headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 401
