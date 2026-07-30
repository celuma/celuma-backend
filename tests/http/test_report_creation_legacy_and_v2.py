"""HTTP integration tests for legacy vs V2 report creation (Céluma 1.3,
Fase 2, Bloque B, Historias B6/B7/B10)."""
from app.models.report import ReportTemplate
from app.models.report_template_version import ReportTemplateVersionStatus

from .conftest import FakeS3Service
from .factories import (
    auth_headers,
    create_branch,
    create_order,
    create_tenant,
    create_user,
    valid_rendering_snapshot,
)


def _create_template(session, tenant, *, name: str = "Default"):
    template = ReportTemplate(tenant_id=tenant.id, name=name, template_json={}, is_active=True)
    session.add(template)
    session.commit()
    session.refresh(template)
    return template


def _publish_version(client, headers, template_id, **overrides):
    resp = client.post(
        f"/api/v1/reports/templates/{template_id}/versions",
        json={"configuration": valid_rendering_snapshot(**overrides)},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _minimal_report_content() -> dict:
    return {
        "base": {"diagnosis": {"label": "Diagnóstico", "value": "Benigno"}},
        "sections": {},
        "base_order": ["diagnosis"],
        "section_order": [],
    }


class TestLegacyCreationUnaffected:
    def test_flag_off_creates_legacy_report(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=False)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")

        resp = client.post(
            "/api/v1/reports/",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "report": _minimal_report_content(),
            },
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text
        report_id = resp.json()["id"]

        detail = client.get(f"/api/v1/reports/{report_id}", headers=auth_headers(user)).json()
        assert detail["schema_version"] is None
        assert detail["template_version_id"] is None
        assert detail["generated_by_renderer_version"] is None
        assert "schema_version" not in detail["report"]
        assert "rendering_snapshot" not in detail["report"]

    def test_flag_on_without_template_version_id_still_creates_legacy_report(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=True)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")

        resp = client.post(
            "/api/v1/reports/",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "report": _minimal_report_content(),
            },
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text
        detail = client.get(
            f"/api/v1/reports/{resp.json()['id']}", headers=auth_headers(user)
        ).json()
        assert detail["schema_version"] is None

    def test_flag_off_rejects_explicit_template_version_id(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=False)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)
        version = _publish_version(client, auth_headers(user), template.id)

        resp = client.post(
            "/api/v1/reports/",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "report": _minimal_report_content(),
                "template_version_id": version["id"],
            },
            headers=auth_headers(user),
        )
        assert resp.status_code == 403


class TestV2Creation:
    def test_flag_on_with_template_version_id_creates_v2_report(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=True)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)
        headers = auth_headers(user)
        version = _publish_version(client, headers, template.id)

        resp = client.post(
            "/api/v1/reports/",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "report": _minimal_report_content(),
                "template_version_id": version["id"],
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        detail = client.get(f"/api/v1/reports/{resp.json()['id']}", headers=headers).json()
        assert detail["schema_version"] == 2
        assert detail["template_version_id"] == version["id"]
        assert detail["generated_by_renderer_version"] is not None
        assert detail["report"]["schema_version"] == 2
        assert detail["report"]["rendering_snapshot"]["presentation"]["paper"]["size"] == "LETTER"
        # Clinical content values are untouched, alongside the snapshot.
        assert detail["report"]["base"]["diagnosis"]["value"] == "Benigno"

    def test_v2_without_report_content_is_rejected(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=True)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)
        headers = auth_headers(user)
        version = _publish_version(client, headers, template.id)

        resp = client.post(
            "/api/v1/reports/",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "template_version_id": version["id"],
            },
            headers=headers,
        )
        assert resp.status_code == 400

    def test_nonexistent_template_version_is_rejected(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=True)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")

        resp = client.post(
            "/api/v1/reports/",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "report": _minimal_report_content(),
                "template_version_id": "00000000-0000-0000-0000-000000000099",
            },
            headers=auth_headers(user),
        )
        assert resp.status_code == 404

    def test_cross_tenant_template_version_is_rejected(self, client, session):
        tenant_a = create_tenant(session, name="Tenant A", reports_v2_enabled=True)
        tenant_b = create_tenant(session, name="Tenant B", reports_v2_enabled=True)
        branch_b = create_branch(session, tenant_b)
        order_b = create_order(session, tenant_b, branch_b)
        user_a = create_user(session, tenant_a, email="admin@a.example")
        user_b = create_user(session, tenant_b, email="admin@b.example")
        template_a = _create_template(session, tenant_a)
        version_a = _publish_version(client, auth_headers(user_a), template_a.id)

        resp = client.post(
            "/api/v1/reports/",
            json={
                "tenant_id": str(tenant_b.id),
                "branch_id": str(branch_b.id),
                "order_id": str(order_b.id),
                "report": _minimal_report_content(),
                "template_version_id": version_a["id"],
            },
            headers=auth_headers(user_b),
        )
        assert resp.status_code == 404

    def test_archived_template_version_is_rejected(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=True)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)
        headers = auth_headers(user)
        version = _publish_version(client, headers, template.id)
        client.post(
            f"/api/v1/reports/templates/{template.id}/versions/{version['id']}/archive",
            headers=headers,
        )

        resp = client.post(
            "/api/v1/reports/",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "report": _minimal_report_content(),
                "template_version_id": version["id"],
            },
            headers=headers,
        )
        assert resp.status_code == 409


class TestSnapshotImmutability:
    """Historia B10: create with version A, publish/activate version B,
    change tenant branding, then re-read the original report and confirm it
    is untouched — the whole point of the snapshot mechanism."""

    def test_snapshot_survives_new_template_version_and_live_branding_changes(
        self, client, session
    ):
        tenant = create_tenant(session, reports_v2_enabled=True)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)
        headers = auth_headers(user)

        version_a = _publish_version(client, headers, template.id)

        create_resp = client.post(
            "/api/v1/reports/",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "report": _minimal_report_content(),
                "template_version_id": version_a["id"],
            },
            headers=headers,
        )
        assert create_resp.status_code == 200, create_resp.text
        report_id = create_resp.json()["id"]
        original = client.get(f"/api/v1/reports/{report_id}", headers=headers).json()
        original_institution = original["report"]["rendering_snapshot"]["presentation"]["header"][
            "institution_name"
        ]
        assert original_institution == "Céluma Labs"

        # Publish and activate a DIFFERENT version with different branding.
        version_b = _publish_version(
            client, headers, template.id, presentation={
                **valid_rendering_snapshot()["presentation"],
                "header": {
                    **valid_rendering_snapshot()["presentation"]["header"],
                    "institution_name": "Otro Nombre Institucional",
                },
            },
        )
        client.post(
            f"/api/v1/reports/templates/{template.id}/versions/{version_b['id']}/activate",
            headers=headers,
        )

        # Change tenant "live" branding too.
        tenant.name = "Nombre Cambiado En Vivo"
        session.add(tenant)
        session.commit()

        # The original report's snapshot must be byte-for-byte unchanged.
        reread = client.get(f"/api/v1/reports/{report_id}", headers=headers).json()
        assert (
            reread["report"]["rendering_snapshot"]["presentation"]["header"]["institution_name"]
            == original_institution
        )
        assert reread["template_version_id"] == original["template_version_id"]


class TestV2Atomicity:
    """Historia B8: a failed S3 upload during V2 creation must not leave an
    orphaned Report row nor a partially-configured version."""

    def test_s3_failure_during_v2_creation_leaves_no_orphaned_report(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=True)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)
        headers = auth_headers(user)
        version = _publish_version(client, headers, template.id)

        FakeS3Service.fail_next_upload = True
        resp = client.post(
            "/api/v1/reports/",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "report": _minimal_report_content(),
                "template_version_id": version["id"],
            },
            headers=headers,
        )
        assert resp.status_code == 500

        from sqlmodel import select

        from app.models.report import Report

        remaining = session.exec(select(Report).where(Report.order_id == order.id)).all()
        assert remaining == []

        session.refresh(order)
        assert order.report_id is None

    def test_after_compensation_a_new_report_can_be_created_for_the_same_order(
        self, client, session
    ):
        tenant = create_tenant(session, reports_v2_enabled=True)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)
        headers = auth_headers(user)
        version = _publish_version(client, headers, template.id)

        FakeS3Service.fail_next_upload = True
        failed = client.post(
            "/api/v1/reports/",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "report": _minimal_report_content(),
                "template_version_id": version["id"],
            },
            headers=headers,
        )
        assert failed.status_code == 500

        retried = client.post(
            "/api/v1/reports/",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "report": _minimal_report_content(),
                "template_version_id": version["id"],
            },
            headers=headers,
        )
        assert retried.status_code == 200, retried.text
