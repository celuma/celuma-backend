"""H-0c addendum — pathologist + reviewer report-editor bootstrap.

The first H-0c remediation moved the template/letterhead VERSION reads from
`reports:manage_templates` to `reports:read`. The operator then reported the
editor still failing for a user holding BOTH `pathologist` and `reviewer`,
with the backend detail `Permission required: reports:manage_templates`.

That report was reproduced and traced to a **stale API process**, not to a
second code defect: the dev container runs `uvicorn` without `--reload` and
had been up for three days, so it was still serving the pre-remediation
module while the frontend had been rebuilt. Restarting the process cleared
it. Evidence is in the H-0c document.

This module exists so that conclusion can never rest on a manual check
again. It walks the editor's real bootstrap sequence, in order, at the API
boundary, as a user holding both roles, and fails if ANY step returns 403 —
so a regression in any single link is attributed to the link that broke.

Neither role holds `reports:manage_templates`:

    pathologist : lab:read, reports:read/create/edit/submit/approve/sign/retract, …
    reviewer    : lab:read, reports:read/approve/sign, lab:manage_reviewers
"""
import pytest

from .factories import (
    add_order_reviewer,
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

# The reported user. Kept as a constant because every test in this module is
# about this exact combination, not about either role alone.
AUTHOR_REVIEWER = ("pathologist", "reviewer")


def _fully_configured_tenant(session, *, name="Test Tenant"):
    """A tenant with nothing missing: V2 on, a template with an ACTIVE
    version, and a default letterhead with an ACTIVE version. Any blocked
    state a role hits here is authorization, never configuration."""
    from app.models.study_type import StudyType
    from app.models.report import ReportTemplate
    from app.models.report_template_version import (
        ReportTemplateVersion,
        ReportTemplateVersionStatus,
    )

    tenant = create_tenant(session, name=name, reports_v2_enabled=True)
    branch = create_branch(session, tenant)

    template = ReportTemplate(tenant_id=tenant.id, name="Clínica")
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

    study_type = StudyType(
        tenant_id=tenant.id,
        code="BIO",
        name="Biopsia",
        default_report_template_id=template.id,
    )
    session.add(study_type)

    letterhead = create_letterhead(session, tenant, name="Predeterminado")
    letterhead.is_default = True
    session.add(letterhead)
    session.commit()

    lh_version = create_letterhead_version(
        session, tenant, letterhead, status="ACTIVE", configuration=valid_presentation()
    )
    session.commit()
    for obj in (template, version, study_type, letterhead, lh_version):
        session.refresh(obj)

    return {
        "tenant": tenant,
        "branch": branch,
        "template": template,
        "version": version,
        "study_type": study_type,
        "letterhead": letterhead,
        "letterhead_version": lh_version,
        "order": create_order(session, tenant, branch),
    }


def _author_reviewer(session, tenant, *, email="author.reviewer@a.example"):
    return create_user(session, tenant, email=email, roles=AUTHOR_REVIEWER)


def _minimal_report_content():
    return {
        "base": {"diagnosis": {"label": "Diagnóstico", "value": "Benigno"}},
        "sections": {},
        "base_order": ["diagnosis"],
        "section_order": [],
    }


class TestAuthorReviewerBootstrapSequence:
    """§8 — the whole sequence, not isolated endpoint permissions."""

    def test_the_complete_editor_bootstrap_never_returns_403(self, client, session):
        env = _fully_configured_tenant(session)
        user = _author_reviewer(session, env["tenant"])
        headers = auth_headers(user)

        # 1. authenticate + read the tenant's V2 flag
        steps = [("tenant flag", "GET", f"/api/v1/tenants/{env['tenant'].id}")]
        resp = client.get(steps[0][2], headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["reports_v2_enabled"] is True

        # 2. the order the report is created from
        resp = client.get(
            f"/api/v1/laboratory/orders/{env['order'].id}", headers=headers
        )
        assert resp.status_code == 200, resp.text

        # 3. effective report defaults (template + resolved letterhead)
        resp = client.get(
            f"/api/v1/study-types/{env['study_type'].id}/report-defaults",
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        defaults = resp.json()
        assert defaults["v2_blocked_reason"] is None
        assert defaults["letterhead_presentation"] is not None

        # 4. the template itself
        resp = client.get(
            f"/api/v1/reports/templates/{defaults['template_id']}", headers=headers
        )
        assert resp.status_code == 200, resp.text

        # 5. the ACTIVE template version — the original H-0c blocker
        resp = client.get(
            f"/api/v1/reports/templates/{defaults['template_id']}"
            f"/versions/{defaults['active_template_version_id']}",
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["configuration"] is not None

        # 6. the letterhead selector chain
        resp = client.get("/api/v1/report-letterheads/", headers=headers)
        assert resp.status_code == 200, resp.text
        for letterhead in resp.json()["letterheads"]:
            versions = client.get(
                f"/api/v1/report-letterheads/{letterhead['id']}/versions",
                headers=headers,
            )
            assert versions.status_code == 200, versions.text
            for v in versions.json()["versions"]:
                detail = client.get(
                    f"/api/v1/report-letterheads/{letterhead['id']}/versions/{v['id']}",
                    headers=headers,
                )
                assert detail.status_code == 200, detail.text

        # 7. initialize the editor by creating the report
        created = client.post(
            "/api/v1/reports/",
            json={
                "tenant_id": str(env["tenant"].id),
                "branch_id": str(env["branch"].id),
                "order_id": str(env["order"].id),
                "title": "Reporte",
                "report": _minimal_report_content(),
                "template_version_id": str(env["version"].id),
            },
            headers=headers,
        )
        assert created.status_code in (200, 201), created.text
        report_id = created.json()["id"]

        # 8. reopen it — the saved-report path loads the same configuration
        for url in (
            f"/api/v1/reports/{report_id}",
            f"/api/v1/reports/{report_id}/full",
            f"/api/v1/reports/{report_id}/versions",
        ):
            resp = client.get(url, headers=headers)
            assert resp.status_code == 200, f"{url}: {resp.text}"

    def test_no_bootstrap_step_is_denied_for_manage_templates(self, client, session):
        """The failure mode stated as an invariant: whatever else may break,
        nothing in the authoring path may demand the administration
        permission. Reports EVERY offending route at once rather than
        stopping at the first."""
        env = _fully_configured_tenant(session)
        user = _author_reviewer(session, env["tenant"])
        headers = auth_headers(user)

        bootstrap_reads = [
            f"/api/v1/tenants/{env['tenant'].id}",
            f"/api/v1/laboratory/orders/{env['order'].id}",
            f"/api/v1/study-types/{env['study_type'].id}",
            f"/api/v1/study-types/{env['study_type'].id}/report-defaults",
            f"/api/v1/reports/templates/{env['template'].id}",
            f"/api/v1/reports/templates/{env['template'].id}/versions",
            f"/api/v1/reports/templates/{env['template'].id}/versions/{env['version'].id}",
            "/api/v1/report-letterheads/",
            f"/api/v1/report-letterheads/{env['letterhead'].id}",
            f"/api/v1/report-letterheads/{env['letterhead'].id}/versions",
            f"/api/v1/report-letterheads/{env['letterhead'].id}/versions/active",
            f"/api/v1/report-letterheads/{env['letterhead'].id}"
            f"/versions/{env['letterhead_version'].id}",
            "/api/v1/report-sections/",
            "/api/v1/reports/worklist",
        ]

        offenders = []
        for url in bootstrap_reads:
            resp = client.get(url, headers=headers)
            if resp.status_code == 403:
                offenders.append((url, resp.text))

        assert offenders == [], (
            "authoring reads denied for a pathologist+reviewer:\n"
            + "\n".join(f"  {u} -> {t}" for u, t in offenders)
        )

    def test_the_report_is_editable_by_the_authoring_role(self, client, session):
        """§8.9 — bootstrap succeeding is not enough; the role must be able to
        act on the report it just opened."""
        env = _fully_configured_tenant(session)
        user = _author_reviewer(session, env["tenant"])
        headers = auth_headers(user)

        created = client.post(
            "/api/v1/reports/",
            json={
                "tenant_id": str(env["tenant"].id),
                "branch_id": str(env["branch"].id),
                "order_id": str(env["order"].id),
                "title": "Reporte",
                "report": _minimal_report_content(),
                "template_version_id": str(env["version"].id),
            },
            headers=headers,
        )
        assert created.status_code in (200, 201), created.text
        report_id = created.json()["id"]

        # `reports:edit` -> a new version may be authored
        content = _minimal_report_content()
        content["base"]["diagnosis"]["value"] = "Maligno"
        resp = client.post(
            f"/api/v1/reports/{report_id}/new_version",
            json={
                "tenant_id": str(env["tenant"].id),
                "branch_id": str(env["branch"].id),
                "order_id": str(env["order"].id),
                "report": content,
            },
            headers=headers,
        )
        assert resp.status_code in (200, 201), resp.text

        # `reports:submit` -> it may be sent for review. Submission requires a
        # reviewer to be assigned (a domain rule, not an authorization one);
        # this user holds `reviewer`, so it reviews its own order here.
        add_order_reviewer(session, env["tenant"], env["order"], user)
        resp = client.post(
            f"/api/v1/reports/{report_id}/submit",
            json={"changelog": "Listo para revisión"},
            headers=headers,
        )
        assert resp.status_code in (200, 201), resp.text


class TestAuthorReviewerTenantIsolation:
    """§7 — the widened reads must not cross a tenant boundary."""

    @pytest.mark.parametrize(
        "path_for",
        [
            pytest.param(
                lambda e: f"/api/v1/reports/templates/{e['template'].id}"
                f"/versions/{e['version'].id}",
                id="template-version",
            ),
            pytest.param(
                lambda e: f"/api/v1/reports/templates/{e['template'].id}/versions",
                id="template-versions-list",
            ),
            pytest.param(
                lambda e: f"/api/v1/reports/templates/{e['template'].id}",
                id="template",
            ),
            pytest.param(
                lambda e: f"/api/v1/report-letterheads/{e['letterhead'].id}"
                f"/versions/{e['letterhead_version'].id}",
                id="letterhead-version",
            ),
            pytest.param(
                lambda e: f"/api/v1/report-letterheads/{e['letterhead'].id}/versions",
                id="letterhead-versions-list",
            ),
            pytest.param(
                lambda e: f"/api/v1/report-letterheads/{e['letterhead'].id}"
                f"/versions/active",
                id="letterhead-active-version",
            ),
            pytest.param(
                lambda e: f"/api/v1/report-letterheads/{e['letterhead'].id}",
                id="letterhead",
            ),
            pytest.param(
                lambda e: f"/api/v1/study-types/{e['study_type'].id}/report-defaults",
                id="report-defaults",
            ),
        ],
    )
    def test_tenant_a_cannot_read_tenant_b(self, client, session, path_for):
        env_a = _fully_configured_tenant(session, name="Tenant A")
        env_b = _fully_configured_tenant(session, name="Tenant B")
        user_a = _author_reviewer(session, env_a["tenant"], email="a@a.example")

        resp = client.get(path_for(env_b), headers=auth_headers(user_a))

        # 404, not 403: existence is not disclosed across tenants.
        assert resp.status_code == 404, resp.text

    @pytest.mark.parametrize(
        "path_for",
        [
            pytest.param(
                lambda e: f"/api/v1/reports/templates/{e['template'].id}"
                f"/versions/{e['version'].id}",
                id="template-version",
            ),
            pytest.param(
                lambda e: f"/api/v1/report-letterheads/{e['letterhead'].id}"
                f"/versions/{e['letterhead_version'].id}",
                id="letterhead-version",
            ),
            pytest.param(
                lambda e: f"/api/v1/study-types/{e['study_type'].id}/report-defaults",
                id="report-defaults",
            ),
        ],
    )
    def test_tenant_a_can_read_its_own(self, client, session, path_for):
        """The positive half — proving the 404s above are isolation and not
        the permission simply being denied everywhere."""
        env_a = _fully_configured_tenant(session, name="Tenant A")
        _fully_configured_tenant(session, name="Tenant B")
        user_a = _author_reviewer(session, env_a["tenant"], email="a@a.example")

        resp = client.get(path_for(env_a), headers=auth_headers(user_a))

        assert resp.status_code == 200, resp.text


class TestAuthorReviewerCannotAdminister:
    """§9 — the widened reads must not become privilege escalation."""

    def _denied(self, resp):
        assert resp.status_code == 403, resp.text
        assert "reports:manage_templates" in resp.text

    def test_cannot_create_a_template(self, client, session):
        env = _fully_configured_tenant(session)
        user = _author_reviewer(session, env["tenant"])
        self._denied(
            client.post(
                "/api/v1/reports/templates/",
                json={"name": "Mía", "template_json": {"base": {}, "sections": {}}},
                headers=auth_headers(user),
            )
        )

    def test_cannot_update_a_template(self, client, session):
        env = _fully_configured_tenant(session)
        user = _author_reviewer(session, env["tenant"])
        self._denied(
            client.put(
                f"/api/v1/reports/templates/{env['template'].id}",
                json={"name": "Renombrada"},
                headers=auth_headers(user),
            )
        )

    def test_cannot_delete_a_template(self, client, session):
        env = _fully_configured_tenant(session)
        user = _author_reviewer(session, env["tenant"])
        self._denied(
            client.delete(
                f"/api/v1/reports/templates/{env['template'].id}",
                headers=auth_headers(user),
            )
        )

    def test_cannot_create_a_template_version(self, client, session):
        env = _fully_configured_tenant(session)
        user = _author_reviewer(session, env["tenant"])
        self._denied(
            client.post(
                f"/api/v1/reports/templates/{env['template'].id}/versions",
                json={"configuration": valid_rendering_snapshot()},
                headers=auth_headers(user),
            )
        )

    def test_cannot_activate_or_archive_a_template_version(self, client, session):
        env = _fully_configured_tenant(session)
        user = _author_reviewer(session, env["tenant"])
        headers = auth_headers(user)
        base = (
            f"/api/v1/reports/templates/{env['template'].id}"
            f"/versions/{env['version'].id}"
        )
        self._denied(client.post(f"{base}/activate", headers=headers))
        self._denied(client.post(f"{base}/archive", headers=headers))

    def test_cannot_mutate_a_letterhead(self, client, session):
        env = _fully_configured_tenant(session)
        user = _author_reviewer(session, env["tenant"])
        headers = auth_headers(user)
        self._denied(
            client.post(
                "/api/v1/report-letterheads/",
                json={"name": "Nuevo"},
                headers=headers,
            )
        )
        self._denied(
            client.put(
                f"/api/v1/report-letterheads/{env['letterhead'].id}",
                json={"name": "Secuestrado"},
                headers=headers,
            )
        )
        self._denied(
            client.delete(
                f"/api/v1/report-letterheads/{env['letterhead'].id}", headers=headers
            )
        )

    def test_cannot_change_the_tenant_default_letterhead(self, client, session):
        env = _fully_configured_tenant(session)
        user = _author_reviewer(session, env["tenant"])
        self._denied(
            client.post(
                f"/api/v1/report-letterheads/{env['letterhead'].id}/default",
                headers=auth_headers(user),
            )
        )

    def test_cannot_save_or_publish_a_letterhead_version(self, client, session):
        env = _fully_configured_tenant(session)
        user = _author_reviewer(session, env["tenant"])
        headers = auth_headers(user)
        self._denied(
            client.put(
                f"/api/v1/report-letterheads/{env['letterhead'].id}/versions/current",
                json={"configuration": valid_presentation()},
                headers=headers,
            )
        )
        self._denied(
            client.post(
                f"/api/v1/report-letterheads/{env['letterhead'].id}/versions",
                json={"configuration": valid_presentation()},
                headers=headers,
            )
        )

    def test_cannot_export_a_letterhead_design(self, client, session):
        """`.cell` export stays administration: it is a portability action on
        the DESIGN, not something authoring a report requires. This is the
        one GET in the letterhead family deliberately left on
        `reports:manage_templates`."""
        env = _fully_configured_tenant(session)
        user = _author_reviewer(session, env["tenant"])
        self._denied(
            client.get(
                f"/api/v1/report-letterheads/{env['letterhead'].id}"
                f"/versions/{env['letterhead_version'].id}/export",
                headers=auth_headers(user),
            )
        )

    def test_cannot_change_tenant_report_settings(self, client, session):
        env = _fully_configured_tenant(session)
        user = _author_reviewer(session, env["tenant"])
        resp = client.patch(
            f"/api/v1/tenants/{env['tenant'].id}",
            json={"reports_v2_enabled": False},
            headers=auth_headers(user),
        )
        assert resp.status_code == 403, resp.text
        assert "admin:manage_tenant" in resp.text

    def test_admin_can_still_do_all_of_it(self, client, session):
        """Guards the negatives above against becoming vacuously true."""
        env = _fully_configured_tenant(session)
        admin = create_user(
            session, env["tenant"], email="admin@a.example", roles=("admin",)
        )
        headers = auth_headers(admin)

        assert client.post(
            f"/api/v1/report-letterheads/{env['letterhead'].id}/default",
            headers=headers,
        ).status_code == 200
        assert client.get(
            f"/api/v1/report-letterheads/{env['letterhead'].id}"
            f"/versions/{env['letterhead_version'].id}/export",
            headers=headers,
        ).status_code == 200
