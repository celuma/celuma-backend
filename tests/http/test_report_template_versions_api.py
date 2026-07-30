"""HTTP integration tests for the ReportTemplateVersion endpoints (Céluma
1.3, Fase 2, Bloque B, Historia B3 + B10)."""
from sqlmodel import Session

from app.models.report import ReportTemplate
from app.models.report_template_version import ReportTemplateVersion, ReportTemplateVersionStatus

from .factories import (
    auth_headers,
    create_branch,
    create_storage_object,
    create_tenant,
    create_user,
    valid_rendering_snapshot,
)


def _create_template(session: Session, tenant, *, name: str = "Default") -> ReportTemplate:
    template = ReportTemplate(tenant_id=tenant.id, name=name, template_json={}, is_active=True)
    session.add(template)
    session.commit()
    session.refresh(template)
    return template


class TestCreateVersion:
    def test_publish_first_version_succeeds(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)

        resp = client.post(
            f"/api/v1/reports/templates/{template.id}/versions",
            json={"configuration": valid_rendering_snapshot()},
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["version_number"] == 1
        assert body["status"] == "PUBLISHED"
        assert body["configuration"]["presentation"]["paper"]["size"] == "LETTER"

    def test_second_version_increments_number(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)

        for _ in range(2):
            resp = client.post(
                f"/api/v1/reports/templates/{template.id}/versions",
                json={"configuration": valid_rendering_snapshot()},
                headers=auth_headers(user),
            )
            assert resp.status_code == 200

        listed = client.get(
            f"/api/v1/reports/templates/{template.id}/versions", headers=auth_headers(user)
        )
        numbers = sorted(v["version_number"] for v in listed.json()["versions"])
        assert numbers == [1, 2]

    def test_invalid_configuration_is_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)

        payload = valid_rendering_snapshot()
        payload["presentation"]["paper"]["size"] = "A4"
        resp = client.post(
            f"/api/v1/reports/templates/{template.id}/versions",
            json={"configuration": payload},
            headers=auth_headers(user),
        )
        assert resp.status_code == 422

    def test_cross_tenant_template_is_rejected(self, client, session):
        tenant_a = create_tenant(session, name="Tenant A")
        tenant_b = create_tenant(session, name="Tenant B")
        user_b = create_user(session, tenant_b, email="admin@b.example")
        template_a = _create_template(session, tenant_a)

        resp = client.post(
            f"/api/v1/reports/templates/{template_a.id}/versions",
            json={"configuration": valid_rendering_snapshot()},
            headers=auth_headers(user_b),
        )
        assert resp.status_code == 404

    def test_missing_permission_is_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="noperm@t1.example", roles=[])
        template = _create_template(session, tenant)

        resp = client.post(
            f"/api/v1/reports/templates/{template.id}/versions",
            json={"configuration": valid_rendering_snapshot()},
            headers=auth_headers(user),
        )
        assert resp.status_code == 403

    def test_nonexistent_template_is_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")

        resp = client.post(
            "/api/v1/reports/templates/00000000-0000-0000-0000-000000000099/versions",
            json={"configuration": valid_rendering_snapshot()},
            headers=auth_headers(user),
        )
        assert resp.status_code == 404

    def test_invalid_logo_reference_is_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)

        payload = valid_rendering_snapshot()
        payload["presentation"]["header"]["logo_storage_id"] = "11111111-1111-1111-1111-111111111111"
        resp = client.post(
            f"/api/v1/reports/templates/{template.id}/versions",
            json={"configuration": payload},
            headers=auth_headers(user),
        )
        assert resp.status_code == 400

    def test_valid_logo_reference_is_accepted(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)
        logo = create_storage_object(session, tenant=tenant)

        payload = valid_rendering_snapshot()
        payload["presentation"]["header"]["logo_storage_id"] = str(logo.id)
        resp = client.post(
            f"/api/v1/reports/templates/{template.id}/versions",
            json={"configuration": payload},
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text

    def test_unowned_logo_reference_is_rejected(self, client, session):
        """A StorageObject with no tenant_id (untagged — e.g. predates this
        scoping, or belongs to an unrelated flow) must not be usable as a
        report-template logo, same as a cross-tenant one (Historia C1)."""
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)
        logo = create_storage_object(session)  # no tenant -> tenant_id stays NULL

        payload = valid_rendering_snapshot()
        payload["presentation"]["header"]["logo_storage_id"] = str(logo.id)
        resp = client.post(
            f"/api/v1/reports/templates/{template.id}/versions",
            json={"configuration": payload},
            headers=auth_headers(user),
        )
        assert resp.status_code == 400

    def test_cross_tenant_logo_reference_is_rejected(self, client, session):
        """A StorageObject owned by a different tenant must be rejected at
        publish time — this is the exact gap Bloque B left open (existence-only
        check) and Bloque C closes. See report-resource-resolution-contract.md."""
        tenant_a = create_tenant(session, name="Tenant A")
        tenant_b = create_tenant(session, name="Tenant B")
        user_a = create_user(session, tenant_a, email="admin@a.example")
        template_a = _create_template(session, tenant_a)
        logo_b = create_storage_object(session, tenant=tenant_b)

        payload = valid_rendering_snapshot()
        payload["presentation"]["header"]["logo_storage_id"] = str(logo_b.id)
        resp = client.post(
            f"/api/v1/reports/templates/{template_a.id}/versions",
            json={"configuration": payload},
            headers=auth_headers(user_a),
        )
        assert resp.status_code == 400


class TestNoUpdateEndpoint:
    def test_put_is_not_supported(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)
        created = client.post(
            f"/api/v1/reports/templates/{template.id}/versions",
            json={"configuration": valid_rendering_snapshot()},
            headers=auth_headers(user),
        ).json()

        resp = client.put(
            f"/api/v1/reports/templates/{template.id}/versions/{created['id']}",
            json={"configuration": valid_rendering_snapshot()},
            headers=auth_headers(user),
        )
        assert resp.status_code in (404, 405)


class TestActivateArchive:
    def _publish(self, client, template_id, headers):
        return client.post(
            f"/api/v1/reports/templates/{template_id}/versions",
            json={"configuration": valid_rendering_snapshot()},
            headers=headers,
        ).json()

    def test_activate_makes_version_the_only_active_one(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)
        headers = auth_headers(user)

        v1 = self._publish(client, template.id, headers)
        v2 = self._publish(client, template.id, headers)

        r1 = client.post(
            f"/api/v1/reports/templates/{template.id}/versions/{v1['id']}/activate",
            headers=headers,
        )
        assert r1.status_code == 200
        assert r1.json()["status"] == "ACTIVE"

        r2 = client.post(
            f"/api/v1/reports/templates/{template.id}/versions/{v2['id']}/activate",
            headers=headers,
        )
        assert r2.status_code == 200
        assert r2.json()["status"] == "ACTIVE"

        # v1 must have been demoted back to PUBLISHED — at most one ACTIVE.
        v1_after = client.get(
            f"/api/v1/reports/templates/{template.id}/versions/{v1['id']}", headers=headers
        ).json()
        assert v1_after["status"] == "PUBLISHED"

    def test_archive_blocks_active_version(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)
        headers = auth_headers(user)

        v1 = self._publish(client, template.id, headers)
        client.post(
            f"/api/v1/reports/templates/{template.id}/versions/{v1['id']}/activate",
            headers=headers,
        )

        resp = client.post(
            f"/api/v1/reports/templates/{template.id}/versions/{v1['id']}/archive",
            headers=headers,
        )
        assert resp.status_code == 409

    def test_archive_then_reactivate_is_allowed(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)
        headers = auth_headers(user)

        v1 = self._publish(client, template.id, headers)
        v2 = self._publish(client, template.id, headers)
        client.post(
            f"/api/v1/reports/templates/{template.id}/versions/{v1['id']}/activate",
            headers=headers,
        )
        archived = client.post(
            f"/api/v1/reports/templates/{template.id}/versions/{v2['id']}/archive",
            headers=headers,
        )
        assert archived.status_code == 200
        assert archived.json()["status"] == "ARCHIVED"

        reactivated = client.post(
            f"/api/v1/reports/templates/{template.id}/versions/{v2['id']}/activate",
            headers=headers,
        )
        assert reactivated.status_code == 200
        assert reactivated.json()["status"] == "ACTIVE"

    def test_archive_already_archived_is_rejected(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)
        headers = auth_headers(user)

        v1 = self._publish(client, template.id, headers)
        first = client.post(
            f"/api/v1/reports/templates/{template.id}/versions/{v1['id']}/archive",
            headers=headers,
        )
        assert first.status_code == 200
        second = client.post(
            f"/api/v1/reports/templates/{template.id}/versions/{v1['id']}/archive",
            headers=headers,
        )
        assert second.status_code == 400


class TestTemplateHardDeleteGuard:
    def test_hard_delete_blocked_when_versions_exist(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)
        headers = auth_headers(user)
        client.post(
            f"/api/v1/reports/templates/{template.id}/versions",
            json={"configuration": valid_rendering_snapshot()},
            headers=headers,
        )

        resp = client.delete(
            f"/api/v1/reports/templates/{template.id}?hard_delete=true", headers=headers
        )
        assert resp.status_code == 409

    def test_soft_delete_still_allowed_when_versions_exist(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)
        headers = auth_headers(user)
        client.post(
            f"/api/v1/reports/templates/{template.id}/versions",
            json={"configuration": valid_rendering_snapshot()},
            headers=headers,
        )

        resp = client.delete(f"/api/v1/reports/templates/{template.id}", headers=headers)
        assert resp.status_code == 200
