"""H-0c — pre-cutover functional blocker: pathologists cannot create reports.

Reproduction first (red), then permanent regression coverage.

The UI reported "Falta el membrete predeterminado del laboratorio" plus
"No se pudo consultar la configuración de reportes V2" for a pathologist.
The letterhead was NOT missing: the report-editor bootstrap chain

    GET /api/v1/tenants/{id}                      (reports_v2_enabled)
    GET /api/v1/study-types/{id}/report-defaults  (lab:read      -> OK)
    GET /api/v1/reports/templates/{tid}/versions/{vid}
                                                  (reports:manage_templates)

fails on its LAST step, because reading the ACTIVE template version — data
required merely to AUTHOR a report — was gated behind the administrator
permission used to MANAGE templates. Pathologists hold `reports:create` and
`reports:edit` but not `reports:manage_templates`, so an admin could
bootstrap a V2 report and a pathologist could not.

The fix separates READ from WRITE: reading a template/letterhead VERSION
requires `reports:read`; creating, activating and archiving versions keeps
requiring `reports:manage_templates`. Tenant anchoring is unchanged — every
lookup still goes through `_get_owned_*`, which 404s across tenants.
"""
import pytest

from .factories import (
    auth_headers,
    create_branch,
    create_letterhead,
    create_letterhead_version,
    create_order,
    create_tenant,
    create_user,
    valid_presentation,
    valid_rendering_snapshot,
)


# Roles that author reports and therefore must be able to READ the effective
# report configuration, and roles that must not gain any of it.
AUTHORING_ROLES = ("pathologist",)
NON_AUTHORING_READ_ROLES = ("lab_tech",)


def _template_with_active_version(session, tenant, *, name="Clínica"):
    from app.models.report import ReportTemplate
    from app.models.report_template_version import (
        ReportTemplateVersion,
        ReportTemplateVersionStatus,
    )

    template = ReportTemplate(tenant_id=tenant.id, name=name)
    session.add(template)
    session.flush()
    version = ReportTemplateVersion(
        tenant_id=tenant.id,
        report_template_id=template.id,
        version_number=1,
        schema_version=2,
        configuration=valid_rendering_snapshot(),
        status=ReportTemplateVersionStatus.ACTIVE,
    )
    session.add(version)
    session.commit()
    session.refresh(template)
    session.refresh(version)
    return template, version


def _study_type_with_template(session, tenant, template, *, code="BIO"):
    from app.models.study_type import StudyType

    study_type = StudyType(
        tenant_id=tenant.id,
        code=code,
        name="Biopsia",
        default_report_template_id=template.id,
    )
    session.add(study_type)
    session.commit()
    session.refresh(study_type)
    return study_type


def _tenant_ready_for_v2(session, *, name="Test Tenant"):
    """A tenant configured exactly as a working lab is: V2 on, a template
    with an ACTIVE version, and a default letterhead with an ACTIVE version.
    Nothing is missing — so any blocked state a role sees is authorization,
    not configuration."""
    tenant = create_tenant(session, name=name, reports_v2_enabled=True)
    branch = create_branch(session, tenant)
    template, version = _template_with_active_version(session, tenant)
    study_type = _study_type_with_template(session, tenant, template)
    letterhead = create_letterhead(session, tenant, name="Predeterminado")
    letterhead.is_default = True
    session.add(letterhead)
    lh_version = create_letterhead_version(
        session, tenant, letterhead, status="ACTIVE", configuration=valid_presentation()
    )
    session.commit()
    return {
        "tenant": tenant,
        "branch": branch,
        "template": template,
        "version": version,
        "study_type": study_type,
        "letterhead": letterhead,
        "letterhead_version": lh_version,
    }


class TestReproductionRoleMatrix:
    """Section 2 of the brief: prove the failing API request per role,
    instead of inferring the cause from the UI message."""

    @pytest.mark.parametrize("role", ["admin", "pathologist"])
    def test_report_defaults_is_readable_by_every_authoring_role(
        self, client, session, role
    ):
        """Step 2 of the bootstrap chain was never the failure: `lab:read`
        is held by admin and pathologist alike."""
        env = _tenant_ready_for_v2(session)
        user = create_user(
            session, env["tenant"], email=f"{role}@a.example", roles=(role,)
        )

        resp = client.get(
            f"/api/v1/study-types/{env['study_type'].id}/report-defaults",
            headers=auth_headers(user),
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["v2_blocked_reason"] is None
        assert body["letterhead_version_id"] == str(env["letterhead_version"].id)
        assert body["active_template_version_id"] == str(env["version"].id)

    @pytest.mark.parametrize("role", ["admin", "pathologist"])
    def test_active_template_version_is_readable_by_every_authoring_role(
        self, client, session, role
    ):
        """THE BLOCKER. Step 3 of the bootstrap chain. Before the fix this
        was 200 for admin and 403 for pathologist, and the editor's single
        `catch` turned that 403 into "Falta el membrete predeterminado"."""
        env = _tenant_ready_for_v2(session)
        user = create_user(
            session, env["tenant"], email=f"{role}@a.example", roles=(role,)
        )

        resp = client.get(
            f"/api/v1/reports/templates/{env['template'].id}"
            f"/versions/{env['version'].id}",
            headers=auth_headers(user),
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["configuration"] is not None

    @pytest.mark.parametrize("role", ["admin", "pathologist"])
    def test_full_v2_bootstrap_chain_succeeds_for_every_authoring_role(
        self, client, session, role
    ):
        """End-to-end reproduction: the exact three requests the report
        editor makes when initializing a new report, in order."""
        env = _tenant_ready_for_v2(session)
        user = create_user(
            session, env["tenant"], email=f"{role}@a.example", roles=(role,)
        )
        headers = auth_headers(user)

        tenant_resp = client.get(
            f"/api/v1/tenants/{env['tenant'].id}", headers=headers
        )
        assert tenant_resp.status_code == 200, tenant_resp.text
        assert tenant_resp.json()["reports_v2_enabled"] is True

        defaults_resp = client.get(
            f"/api/v1/study-types/{env['study_type'].id}/report-defaults",
            headers=headers,
        )
        assert defaults_resp.status_code == 200, defaults_resp.text
        defaults = defaults_resp.json()
        assert defaults["v2_blocked_reason"] is None

        version_resp = client.get(
            f"/api/v1/reports/templates/{defaults['template_id']}"
            f"/versions/{defaults['active_template_version_id']}",
            headers=headers,
        )
        assert version_resp.status_code == 200, version_resp.text

    def test_pathologist_can_create_a_v2_report(self, client, session):
        """Section 2.B — the user-visible outcome the blocker prevented."""
        env = _tenant_ready_for_v2(session)
        user = create_user(
            session,
            env["tenant"],
            email="pathologist@a.example",
            roles=("pathologist",),
        )
        order = create_order(session, env["tenant"], env["branch"])

        resp = client.post(
            "/api/v1/reports/",
            json={
                "tenant_id": str(env["tenant"].id),
                "branch_id": str(env["branch"].id),
                "order_id": str(order.id),
                "title": "Reporte",
                "report": {
                    "base": {"diagnosis": {"label": "Diagnóstico", "value": "Benigno"}},
                    "sections": {},
                    "base_order": ["diagnosis"],
                    "section_order": [],
                },
                "template_version_id": str(env["version"].id),
            },
            headers=auth_headers(user),
        )

        assert resp.status_code in (200, 201), resp.text


class TestReadWriteSeparation:
    """Section 4: reading the effective configuration must NOT imply
    permission to administer it."""

    def test_pathologist_cannot_create_a_template_version(self, client, session):
        env = _tenant_ready_for_v2(session)
        user = create_user(
            session,
            env["tenant"],
            email="pathologist@a.example",
            roles=("pathologist",),
        )

        resp = client.post(
            f"/api/v1/reports/templates/{env['template'].id}/versions",
            json={"configuration": valid_rendering_snapshot()},
            headers=auth_headers(user),
        )

        assert resp.status_code == 403, resp.text
        assert "reports:manage_templates" in resp.text

    def test_pathologist_cannot_activate_a_template_version(self, client, session):
        env = _tenant_ready_for_v2(session)
        user = create_user(
            session,
            env["tenant"],
            email="pathologist@a.example",
            roles=("pathologist",),
        )

        resp = client.post(
            f"/api/v1/reports/templates/{env['template'].id}"
            f"/versions/{env['version'].id}/activate",
            headers=auth_headers(user),
        )

        assert resp.status_code == 403, resp.text
        assert "reports:manage_templates" in resp.text

    def test_pathologist_cannot_archive_a_template_version(self, client, session):
        env = _tenant_ready_for_v2(session)
        user = create_user(
            session,
            env["tenant"],
            email="pathologist@a.example",
            roles=("pathologist",),
        )

        resp = client.post(
            f"/api/v1/reports/templates/{env['template'].id}"
            f"/versions/{env['version'].id}/archive",
            headers=auth_headers(user),
        )

        assert resp.status_code == 403, resp.text
        assert "reports:manage_templates" in resp.text

    def test_pathologist_cannot_mutate_a_letterhead(self, client, session):
        env = _tenant_ready_for_v2(session)
        user = create_user(
            session,
            env["tenant"],
            email="pathologist@a.example",
            roles=("pathologist",),
        )

        resp = client.put(
            f"/api/v1/report-letterheads/{env['letterhead'].id}",
            json={"name": "Secuestrado"},
            headers=auth_headers(user),
        )

        assert resp.status_code == 403, resp.text
        assert "reports:manage_templates" in resp.text

    def test_pathologist_cannot_change_the_tenant_default_letterhead(
        self, client, session
    ):
        env = _tenant_ready_for_v2(session)
        other = create_letterhead(session, env["tenant"], name="Otro")
        create_letterhead_version(session, env["tenant"], other, status="ACTIVE")
        session.commit()
        user = create_user(
            session,
            env["tenant"],
            email="pathologist@a.example",
            roles=("pathologist",),
        )

        resp = client.post(
            f"/api/v1/report-letterheads/{other.id}/default",
            headers=auth_headers(user),
        )

        assert resp.status_code == 403, resp.text
        assert "reports:manage_templates" in resp.text

    def test_pathologist_cannot_change_tenant_report_settings(self, client, session):
        """`reports_v2_enabled` lives on the tenant and stays admin-only."""
        env = _tenant_ready_for_v2(session)
        user = create_user(
            session,
            env["tenant"],
            email="pathologist@a.example",
            roles=("pathologist",),
        )

        resp = client.patch(
            f"/api/v1/tenants/{env['tenant'].id}",
            json={"reports_v2_enabled": False},
            headers=auth_headers(user),
        )

        assert resp.status_code == 403, resp.text
        assert "admin:manage_tenant" in resp.text


class TestTenantIsolation:
    """Section 5: widening a read must never widen it across tenants."""

    def test_pathologist_cannot_read_another_tenants_template_version(
        self, client, session
    ):
        env_a = _tenant_ready_for_v2(session, name="Tenant A")
        env_b = _tenant_ready_for_v2(session, name="Tenant B")
        user_a = create_user(
            session, env_a["tenant"], email="path-a@a.example", roles=("pathologist",)
        )

        resp = client.get(
            f"/api/v1/reports/templates/{env_b['template'].id}"
            f"/versions/{env_b['version'].id}",
            headers=auth_headers(user_a),
        )

        assert resp.status_code == 404, resp.text

    def test_pathologist_cannot_list_another_tenants_template_versions(
        self, client, session
    ):
        env_a = _tenant_ready_for_v2(session, name="Tenant A")
        env_b = _tenant_ready_for_v2(session, name="Tenant B")
        user_a = create_user(
            session, env_a["tenant"], email="path-a@a.example", roles=("pathologist",)
        )

        resp = client.get(
            f"/api/v1/reports/templates/{env_b['template'].id}/versions",
            headers=auth_headers(user_a),
        )

        assert resp.status_code == 404, resp.text

    def test_pathologist_cannot_read_another_tenants_letterhead_version(
        self, client, session
    ):
        env_a = _tenant_ready_for_v2(session, name="Tenant A")
        env_b = _tenant_ready_for_v2(session, name="Tenant B")
        user_a = create_user(
            session, env_a["tenant"], email="path-a@a.example", roles=("pathologist",)
        )

        resp = client.get(
            f"/api/v1/report-letterheads/{env_b['letterhead'].id}"
            f"/versions/{env_b['letterhead_version'].id}",
            headers=auth_headers(user_a),
        )

        assert resp.status_code == 404, resp.text

    def test_pathologist_cannot_read_another_tenants_report_defaults(
        self, client, session
    ):
        env_a = _tenant_ready_for_v2(session, name="Tenant A")
        env_b = _tenant_ready_for_v2(session, name="Tenant B")
        user_a = create_user(
            session, env_a["tenant"], email="path-a@a.example", roles=("pathologist",)
        )

        resp = client.get(
            f"/api/v1/study-types/{env_b['study_type'].id}/report-defaults",
            headers=auth_headers(user_a),
        )

        assert resp.status_code == 404, resp.text

    def test_pathologist_reads_their_own_tenants_configuration(self, client, session):
        """The positive half of the isolation pair."""
        env_a = _tenant_ready_for_v2(session, name="Tenant A")
        _tenant_ready_for_v2(session, name="Tenant B")
        user_a = create_user(
            session, env_a["tenant"], email="path-a@a.example", roles=("pathologist",)
        )

        resp = client.get(
            f"/api/v1/reports/templates/{env_a['template'].id}"
            f"/versions/{env_a['version'].id}",
            headers=auth_headers(user_a),
        )

        assert resp.status_code == 200, resp.text

    def test_admin_still_manages_their_own_tenant_configuration(self, client, session):
        env_a = _tenant_ready_for_v2(session, name="Tenant A")
        admin = create_user(
            session, env_a["tenant"], email="admin-a@a.example", roles=("admin",)
        )

        resp = client.post(
            f"/api/v1/report-letterheads/{env_a['letterhead'].id}/default",
            headers=auth_headers(admin),
        )

        assert resp.status_code == 200, resp.text


class TestAdjacentConfigurationReads:
    """Section 8, bounded audit: the report-creation flow's OTHER
    configuration reads, so fixing the first request does not expose the
    next admin-only read one step later. The letterhead selector chain is
    `catch`-swallowed in the editor, so a 403 there silently degraded the
    pathologist's editor instead of failing loudly."""

    def test_pathologist_can_list_letterheads(self, client, session):
        env = _tenant_ready_for_v2(session)
        user = create_user(
            session, env["tenant"], email="path@a.example", roles=("pathologist",)
        )

        resp = client.get("/api/v1/report-letterheads/", headers=auth_headers(user))

        assert resp.status_code == 200, resp.text

    def test_pathologist_can_list_letterhead_versions(self, client, session):
        env = _tenant_ready_for_v2(session)
        user = create_user(
            session, env["tenant"], email="path@a.example", roles=("pathologist",)
        )

        resp = client.get(
            f"/api/v1/report-letterheads/{env['letterhead'].id}/versions",
            headers=auth_headers(user),
        )

        assert resp.status_code == 200, resp.text

    def test_pathologist_can_read_a_letterhead_version(self, client, session):
        env = _tenant_ready_for_v2(session)
        user = create_user(
            session, env["tenant"], email="path@a.example", roles=("pathologist",)
        )

        resp = client.get(
            f"/api/v1/report-letterheads/{env['letterhead'].id}"
            f"/versions/{env['letterhead_version'].id}",
            headers=auth_headers(user),
        )

        assert resp.status_code == 200, resp.text

    def test_pathologist_can_read_the_active_letterhead_version(self, client, session):
        env = _tenant_ready_for_v2(session)
        user = create_user(
            session, env["tenant"], email="path@a.example", roles=("pathologist",)
        )

        resp = client.get(
            f"/api/v1/report-letterheads/{env['letterhead'].id}/versions/active",
            headers=auth_headers(user),
        )

        assert resp.status_code == 200, resp.text

    def test_pathologist_can_list_template_versions(self, client, session):
        env = _tenant_ready_for_v2(session)
        user = create_user(
            session, env["tenant"], email="path@a.example", roles=("pathologist",)
        )

        resp = client.get(
            f"/api/v1/reports/templates/{env['template'].id}/versions",
            headers=auth_headers(user),
        )

        assert resp.status_code == 200, resp.text

    def test_pathologist_can_read_the_template(self, client, session):
        env = _tenant_ready_for_v2(session)
        user = create_user(
            session, env["tenant"], email="path@a.example", roles=("pathologist",)
        )

        resp = client.get(
            f"/api/v1/reports/templates/{env['template'].id}",
            headers=auth_headers(user),
        )

        assert resp.status_code == 200, resp.text


class TestUnauthorizedRolesRemainDenied:
    """Section 7.5 — widening to `reports:read` must not widen to everyone."""

    def test_role_without_reports_read_is_denied_the_template_version(
        self, client, session
    ):
        """`physician` is a portal role and holds no `reports:*` permission."""
        env = _tenant_ready_for_v2(session)
        user = create_user(
            session, env["tenant"], email="physician@a.example", roles=("physician",)
        )

        resp = client.get(
            f"/api/v1/reports/templates/{env['template'].id}"
            f"/versions/{env['version'].id}",
            headers=auth_headers(user),
        )

        assert resp.status_code == 403, resp.text
        assert "reports:read" in resp.text

    def test_role_without_reports_read_is_denied_the_letterhead_version(
        self, client, session
    ):
        env = _tenant_ready_for_v2(session)
        user = create_user(
            session, env["tenant"], email="physician@a.example", roles=("physician",)
        )

        resp = client.get(
            f"/api/v1/report-letterheads/{env['letterhead'].id}"
            f"/versions/{env['letterhead_version'].id}",
            headers=auth_headers(user),
        )

        assert resp.status_code == 403, resp.text
        assert "reports:read" in resp.text

    def test_unauthenticated_request_is_rejected(self, client, session):
        env = _tenant_ready_for_v2(session)

        resp = client.get(
            f"/api/v1/reports/templates/{env['template'].id}"
            f"/versions/{env['version'].id}"
        )

        assert resp.status_code in (401, 403), resp.text
