"""HTTP integration tests for report resource resolution (Céluma 1.3, Phase
2, Block C, Story C1). Covers turning a V2 report's
`rendering_snapshot.presentation.header.logo_storage_id` into a URL exposed
via `ReportDetailResponse.resolved_resources`, without ever writing that URL
back into the snapshot, and without ever exposing a cross-tenant object.
"""
import json

from app.models.report import Report, ReportTemplate, ReportVersion

from .conftest import FakeS3Service
from .factories import (
    auth_headers,
    create_branch,
    create_order,
    create_storage_object,
    create_default_letterhead,
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


class TestLogoResolutionOnRead:
    def test_v2_report_with_owned_logo_resolves_url(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=True)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)
        headers = auth_headers(user)
        logo = create_storage_object(session, key="logos/t1-logo.png", tenant=tenant)

        # Third remediation: the logo lives on the LETTERHEAD, not the
        # template version — the report's `presentation` is now always
        # supplied by the resolved letterhead (see
        # deterministic-letterhead-resolution-contract.md). What this test
        # covers, resolving `logo_storage_id` -> URL without writing it into
        # the snapshot, is identical.
        create_default_letterhead(
            session,
            tenant,
            configuration={
                **valid_rendering_snapshot()["presentation"],
                "header": {
                    **valid_rendering_snapshot()["presentation"]["header"],
                    "logo_storage_id": str(logo.id),
                },
            },
        )
        version = _publish_version(client, headers, template.id)

        create_resp = client.post(
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
        assert create_resp.status_code == 200, create_resp.text
        report_id = create_resp.json()["id"]

        detail = client.get(f"/api/v1/reports/{report_id}", headers=headers).json()
        assert detail["resolved_resources"]["header_logo_url"] == "https://fake-cdn.example/logos/t1-logo.png"
        # The snapshot itself keeps the raw storage id, never the resolved URL.
        assert (
            detail["report"]["rendering_snapshot"]["presentation"]["header"]["logo_storage_id"]
            == str(logo.id)
        )

    def test_v2_report_without_logo_has_no_resolved_url(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=True)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)
        headers = auth_headers(user)
        create_default_letterhead(session, tenant)
        version = _publish_version(client, headers, template.id)

        create_resp = client.post(
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
        assert create_resp.status_code == 200, create_resp.text

        detail = client.get(
            f"/api/v1/reports/{create_resp.json()['id']}", headers=headers
        ).json()
        # Nothing to resolve -> the whole resolved_resources object is
        # absent (same shape as a legacy report), not present-with-nulls.
        assert detail["resolved_resources"] is None

    def test_legacy_report_has_no_resolved_resources(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=False)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")
        headers = auth_headers(user)

        create_resp = client.post(
            "/api/v1/reports/",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "report": _minimal_report_content(),
            },
            headers=headers,
        )
        assert create_resp.status_code == 200, create_resp.text

        detail = client.get(
            f"/api/v1/reports/{create_resp.json()['id']}", headers=headers
        ).json()
        assert detail["resolved_resources"] is None

    def test_no_unnecessary_s3_public_url_call_without_logo(self, client, session, monkeypatch):
        """A report with nothing to resolve must not touch S3.object_public_url at all."""
        tenant = create_tenant(session, reports_v2_enabled=True)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")
        template = _create_template(session, tenant)
        headers = auth_headers(user)
        create_default_letterhead(session, tenant)
        version = _publish_version(client, headers, template.id)

        create_resp = client.post(
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
        report_id = create_resp.json()["id"]

        calls: list[str] = []
        original = FakeS3Service.object_public_url

        def _tracking_object_public_url(self, key):
            calls.append(key)
            return original(self, key)

        monkeypatch.setattr(FakeS3Service, "object_public_url", _tracking_object_public_url)

        resp = client.get(f"/api/v1/reports/{report_id}", headers=headers)
        assert resp.status_code == 200
        assert calls == []


class TestCrossTenantLogoNeverLeaks:
    """Defense in depth (Story C1): even if a cross-tenant
    `logo_storage_id` ended up embedded in a snapshot (e.g. a row that
    predates the publish-time tenant check, or a future regression in that
    check), reading the report must never resolve or expose that object's
    URL, and must never raise — it degrades to no logo."""

    def test_cross_tenant_logo_in_snapshot_resolves_to_none(self, client, session):
        tenant_a = create_tenant(session, name="Tenant A", reports_v2_enabled=True)
        tenant_b = create_tenant(session, name="Tenant B")
        branch_a = create_branch(session, tenant_a)
        order_a = create_order(session, tenant_a, branch_a)
        user_a = create_user(session, tenant_a, email="admin@a.example")
        logo_b = create_storage_object(session, key="logos/t2-private.png", tenant=tenant_b)

        # Build a V2 report body directly (bypassing create_template_version's
        # tenant check) whose snapshot references tenant B's private logo,
        # simulating corrupted/legacy data or a future regression.
        snapshot = valid_rendering_snapshot()
        snapshot["presentation"]["header"]["logo_storage_id"] = str(logo_b.id)
        body = {**_minimal_report_content(), "schema_version": 2, "rendering_snapshot": snapshot}

        json_key = f"reports/{tenant_a.id}/{branch_a.id}/manual/versions/1/report.json"
        FakeS3Service.store[json_key] = json.dumps(body).encode("utf-8")
        json_storage = create_storage_object(session, key=json_key, tenant=tenant_a)

        report = Report(tenant_id=tenant_a.id, branch_id=branch_a.id, order_id=order_a.id)
        session.add(report)
        session.flush()
        # Note: `ReportVersion.schema_version` is left unset here — the DB
        # CHECK constraint `ck_report_version_v2_requires_template_version`
        # requires a `template_version_id` whenever it is 2, and this test
        # deliberately has none (it simulates a corrupted/legacy snapshot,
        # not a legitimately-created V2 report). `_resolve_report_resources`
        # only inspects the JSON body's `rendering_snapshot`, not this
        # column, so the security property under test is unaffected.
        version = ReportVersion(
            report_id=report.id,
            version_no=1,
            json_storage_id=json_storage.id,
            is_current=True,
        )
        session.add(version)
        session.commit()

        detail = client.get(
            f"/api/v1/reports/{report.id}", headers=auth_headers(user_a)
        ).json()
        assert detail["resolved_resources"] is None
        # The snapshot itself is passed through untouched (read-only view) —
        # only the *resolved URL* is withheld, not the raw stored reference.
        assert (
            detail["report"]["rendering_snapshot"]["presentation"]["header"]["logo_storage_id"]
            == str(logo_b.id)
        )
