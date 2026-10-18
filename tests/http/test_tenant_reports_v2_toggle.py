"""HTTP integration tests for the RETIRED `reports_v2_enabled` tenant field
(introduced in Céluma 1.3, Phase 2, Block D, Story D9; retired in 1.4, #22).

The field is still parsed by `PATCH /api/v1/tenants/{id}` so older clients get a
precise answer: `true` is accepted and ignored, `false` is refused (409), reads
always report `true`, and nothing about existing reports changes. The endpoint
keeps its `admin:manage_tenant` gate and tenant scope. The creation-path
consequences are covered in `test_reports_v2_only.py`.
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


class TestRetiredReportsV2Field:
    def test_enabling_is_accepted_and_changes_nothing(self, client, session):
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

    def test_disabling_is_refused(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=True)
        user = create_user(session, tenant, email="admin@t1.example")

        resp = client.patch(
            f"/api/v1/tenants/{tenant.id}",
            json={"reports_v2_enabled": False},
            headers=auth_headers(user),
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "reports_v2_flag_retired"

        confirm = client.get(f"/api/v1/tenants/{tenant.id}", headers=auth_headers(user))
        assert confirm.json()["reports_v2_enabled"] is True

    def test_omitting_the_field_leaves_the_tenant_update_working(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=True)
        user = create_user(session, tenant, email="admin@t1.example")

        resp = client.patch(
            f"/api/v1/tenants/{tenant.id}",
            json={"name": "Renamed Tenant"},
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "Renamed Tenant"
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
            json={"reports_v2_enabled": True, "name": "Hijacked"},
            headers=auth_headers(user_b),
        )
        assert resp.status_code == 403

        confirm = client.get(
            f"/api/v1/tenants/{tenant_a.id}",
            headers=auth_headers(create_user(session, tenant_a, email="admin@a.example")),
        )
        assert confirm.json()["name"] == "Tenant A"

    def test_refused_disable_does_not_affect_reading_an_existing_v2_report(
        self, client, session
    ):
        tenant = create_tenant(session, reports_v2_enabled=True)
        user = create_user(session, tenant, email="admin@t1.example")
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        template_version = _create_template_version(session, tenant)
        report, _version = create_published_v2_report_directly(
            session, tenant, branch, order, template_version, status=ReportStatus.PUBLISHED
        )

        toggle = client.patch(
            f"/api/v1/tenants/{tenant.id}",
            json={"reports_v2_enabled": False},
            headers=auth_headers(user),
        )
        assert toggle.status_code == 409

        detail = client.get(f"/api/v1/reports/{report.id}", headers=auth_headers(user))
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["schema_version"] == 2
        assert body["template_version_id"] == str(template_version.id)
