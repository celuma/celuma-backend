"""HTTP integration tests for the `reports_v2_enabled` tenant toggle
(Céluma 1.3, Fase 2, Bloque D, Historia D9).

There is no dedicated endpoint for this flag — it is exposed as an optional
field on the existing `PATCH /api/v1/tenants/{id}` (see
app/api/v1/tenants.py::update_tenant), gated by the same `admin:manage_tenant`
permission as the rest of that endpoint.
"""
from app.models.enums import ReportStatus
from app.models.report import ReportTemplate
from app.models.report_template_version import ReportTemplateVersion

from .factories import (
    auth_headers,
    create_branch,
    create_order,
    create_published_v2_report_directly,
    create_tenant,
    create_user,
    valid_rendering_snapshot,
)


def _create_template_version(session, tenant) -> ReportTemplateVersion:
    template = ReportTemplate(tenant_id=tenant.id, name="Default", template_json={}, is_active=True)
    session.add(template)
    session.flush()
    version = ReportTemplateVersion(
        tenant_id=tenant.id,
        report_template_id=template.id,
        version_number=1,
        configuration=valid_rendering_snapshot(),
    )
    session.add(version)
    session.commit()
    session.refresh(version)
    return version


class TestToggleReportsV2Enabled:
    def test_authorized_user_can_enable(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=False)
        user = create_user(session, tenant, email="admin@t1.example")

        resp = client.patch(
            f"/api/v1/tenants/{tenant.id}",
            json={"reports_v2_enabled": True},
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["reports_v2_enabled"] is True

        confirm = client.get(f"/api/v1/tenants/{tenant.id}", headers=auth_headers(user))
        assert confirm.json()["reports_v2_enabled"] is True

    def test_authorized_user_can_disable(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=True)
        user = create_user(session, tenant, email="admin@t1.example")

        resp = client.patch(
            f"/api/v1/tenants/{tenant.id}",
            json={"reports_v2_enabled": False},
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["reports_v2_enabled"] is False

    def test_omitting_the_field_leaves_flag_unchanged(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=True)
        user = create_user(session, tenant, email="admin@t1.example")

        resp = client.patch(
            f"/api/v1/tenants/{tenant.id}",
            json={"name": "Renamed Tenant"},
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["reports_v2_enabled"] is True

    def test_missing_permission_is_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="viewer@t1.example", roles=[])

        resp = client.patch(
            f"/api/v1/tenants/{tenant.id}",
            json={"reports_v2_enabled": True},
            headers=auth_headers(user),
        )
        assert resp.status_code == 403

    def test_cross_tenant_update_is_rejected(self, client, session):
        tenant_a = create_tenant(session, name="Tenant A")
        tenant_b = create_tenant(session, name="Tenant B")
        user_b = create_user(session, tenant_b, email="admin@b.example")

        resp = client.patch(
            f"/api/v1/tenants/{tenant_a.id}",
            json={"reports_v2_enabled": True},
            headers=auth_headers(user_b),
        )
        assert resp.status_code == 403

        # Tenant A's flag must be untouched.
        confirm = client.get(
            f"/api/v1/tenants/{tenant_a.id}",
            headers=auth_headers(create_user(session, tenant_a, email="admin@a.example")),
        )
        assert confirm.json()["reports_v2_enabled"] is False

    def test_disabling_does_not_affect_reading_an_existing_v2_report(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=True)
        user = create_user(session, tenant, email="admin@t1.example")
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        template_version = _create_template_version(session, tenant)
        report, _version = create_published_v2_report_directly(
            session, tenant, branch, order, template_version, status=ReportStatus.PUBLISHED
        )

        # Turn the flag off.
        toggle = client.patch(
            f"/api/v1/tenants/{tenant.id}",
            json={"reports_v2_enabled": False},
            headers=auth_headers(user),
        )
        assert toggle.status_code == 200
        assert toggle.json()["reports_v2_enabled"] is False

        # The existing V2 report must still read as schema_version 2,
        # unaffected by the flag — it only gates *creation* of new V2 reports.
        detail = client.get(f"/api/v1/reports/{report.id}", headers=auth_headers(user))
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["schema_version"] == 2
        assert body["template_version_id"] == str(template_version.id)

    def test_disabling_does_not_affect_other_tenants(self, client, session):
        tenant_a = create_tenant(session, name="Tenant A", reports_v2_enabled=True)
        tenant_b = create_tenant(session, name="Tenant B", reports_v2_enabled=True)
        user_a = create_user(session, tenant_a, email="admin@a.example")
        user_b = create_user(session, tenant_b, email="admin@b.example")

        resp = client.patch(
            f"/api/v1/tenants/{tenant_a.id}",
            json={"reports_v2_enabled": False},
            headers=auth_headers(user_a),
        )
        assert resp.status_code == 200
        assert resp.json()["reports_v2_enabled"] is False

        confirm_b = client.get(f"/api/v1/tenants/{tenant_b.id}", headers=auth_headers(user_b))
        assert confirm_b.json()["reports_v2_enabled"] is True
