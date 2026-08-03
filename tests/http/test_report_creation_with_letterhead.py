"""HTTP integration tests for letterhead resolution during V2
report creation — post-Phase-2 remediation, R7/R15.

Resolution order under test: explicit letterhead_version_id -> template's
preferred_letterhead_version_id -> tenant's default letterhead's ACTIVE
version -> fall back to the template version's own embedded `presentation`
(never blocks V2 creation — this is what keeps tenants that have not
adopted the letterhead domain yet byte-for-byte unchanged)."""
from app.models.report import ReportTemplate

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


def _create_template(session, tenant, *, name: str = "Default", preferred_letterhead_version_id=None):
    template = ReportTemplate(
        tenant_id=tenant.id,
        name=name,
        template_json={},
        is_active=True,
        preferred_letterhead_version_id=preferred_letterhead_version_id,
    )
    session.add(template)
    session.commit()
    session.refresh(template)
    return template


def _publish_template_version(client, headers, template_id, **overrides):
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


def _create_report(client, headers, tenant, branch, order, template_version_id, letterhead_version_id=None):
    payload = {
        "tenant_id": str(tenant.id),
        "branch_id": str(branch.id),
        "order_id": str(order.id),
        "report": _minimal_report_content(),
        "template_version_id": template_version_id,
    }
    if letterhead_version_id is not None:
        payload["letterhead_version_id"] = letterhead_version_id
    return client.post("/api/v1/reports/", json=payload, headers=headers)


class TestExplicitLetterheadSelection:
    def test_explicit_letterhead_overrides_template_presentation(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=True)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")
        headers = auth_headers(user)
        template = _create_template(session, tenant)
        template_version = _publish_template_version(
            client, headers, template.id,
            presentation={**valid_rendering_snapshot()["presentation"], "style": {"primary_color": "#111111"}},
        )

        letterhead = create_letterhead(session, tenant, name="Explicit Letterhead")
        lh_version = create_letterhead_version(
            session, tenant, letterhead,
            configuration=valid_presentation(style={"primary_color": "#ABCDEF"}),
        )

        resp = _create_report(
            client, headers, tenant, branch, order,
            template_version["id"], str(lh_version.id),
        )
        assert resp.status_code == 200, resp.text

        detail = client.get(f"/api/v1/reports/{resp.json()['id']}", headers=headers).json()
        assert detail["letterhead_version_id"] == str(lh_version.id)
        assert (
            detail["report"]["rendering_snapshot"]["presentation"]["style"]["primary_color"] == "#ABCDEF"
        )
        # Clinical structure came from the template version, unaffected by the letterhead choice.
        assert detail["report"]["rendering_snapshot"]["template"] is not None

    def test_archived_letterhead_version_is_rejected(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=True)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")
        headers = auth_headers(user)
        template = _create_template(session, tenant)
        template_version = _publish_template_version(client, headers, template.id)

        letterhead = create_letterhead(session, tenant)
        lh_version = create_letterhead_version(session, tenant, letterhead, status="ARCHIVED")

        resp = _create_report(
            client, headers, tenant, branch, order, template_version["id"], str(lh_version.id)
        )
        assert resp.status_code == 409

    def test_cross_tenant_letterhead_version_is_rejected(self, client, session):
        tenant_a = create_tenant(session, name="Tenant A", reports_v2_enabled=True)
        tenant_b = create_tenant(session, name="Tenant B")
        branch = create_branch(session, tenant_a)
        order = create_order(session, tenant_a, branch)
        user = create_user(session, tenant_a, email="admin@a.example")
        headers = auth_headers(user)
        template = _create_template(session, tenant_a)
        template_version = _publish_template_version(client, headers, template.id)

        other_letterhead = create_letterhead(session, tenant_b)
        other_version = create_letterhead_version(session, tenant_b, other_letterhead)

        resp = _create_report(
            client, headers, tenant_a, branch, order, template_version["id"], str(other_version.id)
        )
        assert resp.status_code == 404


class TestAutomaticLetterheadResolution:
    def test_falls_back_to_template_preference(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=True)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")
        headers = auth_headers(user)

        letterhead = create_letterhead(session, tenant, name="Template's Preferred")
        lh_version = create_letterhead_version(
            session, tenant, letterhead, configuration=valid_presentation(style={"primary_color": "#222222"})
        )
        template = _create_template(session, tenant, preferred_letterhead_version_id=lh_version.id)
        template_version = _publish_template_version(client, headers, template.id)

        resp = _create_report(client, headers, tenant, branch, order, template_version["id"])
        assert resp.status_code == 200, resp.text

        detail = client.get(f"/api/v1/reports/{resp.json()['id']}", headers=headers).json()
        assert detail["letterhead_version_id"] == str(lh_version.id)
        assert detail["report"]["rendering_snapshot"]["presentation"]["style"]["primary_color"] == "#222222"

    def test_falls_back_to_tenant_default_when_no_template_preference(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=True)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")
        headers = auth_headers(user)

        default_letterhead = create_letterhead(session, tenant, name="Tenant Default")
        default_letterhead.is_default = True
        session.add(default_letterhead)
        session.commit()
        lh_version = create_letterhead_version(
            session, tenant, default_letterhead,
            status="ACTIVE",
            configuration=valid_presentation(style={"primary_color": "#333333"}),
        )

        template = _create_template(session, tenant)  # no preference
        template_version = _publish_template_version(client, headers, template.id)

        resp = _create_report(client, headers, tenant, branch, order, template_version["id"])
        assert resp.status_code == 200, resp.text

        detail = client.get(f"/api/v1/reports/{resp.json()['id']}", headers=headers).json()
        assert detail["letterhead_version_id"] == str(lh_version.id)
        assert detail["report"]["rendering_snapshot"]["presentation"]["style"]["primary_color"] == "#333333"

    def test_no_letterhead_resolvable_blocks_v2_creation_explicitly(self, client, session):
        """DELIBERATE REVERSAL of the second remediation (see
        deterministic-letterhead-resolution-contract.md, "Behavior
        change").

        Previously, a tenant with no letterhead silently created the V2
        report with the `presentation` embedded in the template version.
        That silence was the exact mechanism behind two brief symptoms: V2
        reports with a letterhead the user never chose, and an editor that,
        with no letterhead to resolve, mounted Legacy. Now it blocks with
        409 and an actionable message — never Legacy, never an implicitly
        chosen default letterhead.
        """
        tenant = create_tenant(session, reports_v2_enabled=True)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")
        headers = auth_headers(user)
        template = _create_template(session, tenant)
        template_version = _publish_template_version(
            client, headers, template.id,
            presentation={**valid_rendering_snapshot()["presentation"], "style": {"primary_color": "#999999"}},
        )

        resp = _create_report(client, headers, tenant, branch, order, template_version["id"])
        assert resp.status_code == 409, resp.text
        assert "membrete" in resp.json()["detail"].lower()

    def test_explicit_selection_wins_over_template_preference_and_tenant_default(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=True)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")
        headers = auth_headers(user)

        default_letterhead = create_letterhead(session, tenant, name="Default")
        default_letterhead.is_default = True
        session.add(default_letterhead)
        session.commit()
        create_letterhead_version(session, tenant, default_letterhead, status="ACTIVE")

        preferred_letterhead = create_letterhead(session, tenant, name="Preferred")
        preferred_version = create_letterhead_version(session, tenant, preferred_letterhead)

        explicit_letterhead = create_letterhead(session, tenant, name="Explicit")
        explicit_version = create_letterhead_version(
            session, tenant, explicit_letterhead,
            configuration=valid_presentation(style={"primary_color": "#EEEEEE"}),
        )

        template = _create_template(session, tenant, preferred_letterhead_version_id=preferred_version.id)
        template_version = _publish_template_version(client, headers, template.id)

        resp = _create_report(
            client, headers, tenant, branch, order, template_version["id"], str(explicit_version.id)
        )
        assert resp.status_code == 200, resp.text
        detail = client.get(f"/api/v1/reports/{resp.json()['id']}", headers=headers).json()
        assert detail["letterhead_version_id"] == str(explicit_version.id)
        assert detail["report"]["rendering_snapshot"]["presentation"]["style"]["primary_color"] == "#EEEEEE"
