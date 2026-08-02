"""HTTP integration tests for content-only new_version continuity on V2
reports (Céluma 1.3, Fase 2, Bloque C, Historia C9).

Covers the gap found while exploring the frontend editor's buildEnvelope():
it rebuilds `report` from the template definition and never includes
`schema_version`/`rendering_snapshot`, so calling `POST /{id}/new_version`
with that payload — exactly what the editor's "Guardar" button sends today —
must not be allowed to silently degrade a V2 report to legacy.
"""
from app.models.report import ReportTemplate

from .factories import (
    auth_headers,
    create_branch,
    create_order,
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


def _minimal_report_content(diagnosis: str = "Benigno") -> dict:
    return {
        "base": {"diagnosis": {"label": "Diagnóstico", "value": diagnosis}},
        "sections": {},
        "base_order": ["diagnosis"],
        "section_order": [],
    }


class TestV2ContentEditPreservesSnapshot:
    def _create_v2_report(self, client, headers, tenant, branch, order, template_id):
        version = _publish_version(client, headers, template_id)
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
        return resp.json()["id"], version

    def test_content_only_payload_without_snapshot_keeps_report_v2(self, client, session):
        """Exactly reproduces what report_editor.tsx's buildEnvelope() sends
        today: a `report` body with clinical content but WITHOUT
        `schema_version`/`rendering_snapshot` — those must be carried
        forward by the backend regardless."""
        tenant = create_tenant(session, reports_v2_enabled=True)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")
        headers = auth_headers(user)
        template = _create_template(session, tenant)
        # Tercera remediación: la creación V2 exige un membrete resoluble.
        create_default_letterhead(session, tenant)
        report_id, version = self._create_v2_report(
            client, headers, tenant, branch, order, template.id
        )

        original = client.get(f"/api/v1/reports/{report_id}", headers=headers).json()
        assert original["schema_version"] == 2
        original_snapshot = original["report"]["rendering_snapshot"]

        # Editor-shaped payload: clinical content changed, no V2 keys at all.
        new_version_resp = client.post(
            f"/api/v1/reports/{report_id}/new_version",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "report": _minimal_report_content("Maligno — actualizado"),
            },
            headers=headers,
        )
        assert new_version_resp.status_code == 200, new_version_resp.text
        assert new_version_resp.json()["schema_version"] == 2
        assert new_version_resp.json()["template_version_id"] == version["id"]

        reread = client.get(f"/api/v1/reports/{report_id}", headers=headers).json()
        assert reread["schema_version"] == 2
        assert reread["template_version_id"] == version["id"]
        assert reread["report"]["schema_version"] == 2
        assert reread["report"]["rendering_snapshot"] == original_snapshot
        # New clinical content was applied — this is a real content edit, not a no-op.
        assert reread["report"]["base"]["diagnosis"]["value"] == "Maligno — actualizado"

    def test_client_supplied_rendering_snapshot_is_ignored_and_replaced_by_the_frozen_one(
        self, client, session
    ):
        """Even if a client DID send a rendering_snapshot on new_version, the
        backend must never trust it — only the already-persisted one is
        authoritative (no re-validation against a live template version)."""
        tenant = create_tenant(session, reports_v2_enabled=True)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")
        headers = auth_headers(user)
        template = _create_template(session, tenant)
        # Tercera remediación: la creación V2 exige un membrete resoluble.
        create_default_letterhead(session, tenant)
        report_id, _version = self._create_v2_report(
            client, headers, tenant, branch, order, template.id
        )
        original = client.get(f"/api/v1/reports/{report_id}", headers=headers).json()
        original_snapshot = original["report"]["rendering_snapshot"]

        tampered_body = {
            **_minimal_report_content(),
            "schema_version": 2,
            "rendering_snapshot": {
                **original_snapshot,
                "presentation": {
                    **original_snapshot["presentation"],
                    "header": {
                        **original_snapshot["presentation"]["header"],
                        "institution_name": "Nombre Falsificado Por El Cliente",
                    },
                },
            },
        }
        resp = client.post(
            f"/api/v1/reports/{report_id}/new_version",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "report": tampered_body,
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text

        reread = client.get(f"/api/v1/reports/{report_id}", headers=headers).json()
        assert reread["report"]["rendering_snapshot"] == original_snapshot
        assert (
            reread["report"]["rendering_snapshot"]["presentation"]["header"]["institution_name"]
            != "Nombre Falsificado Por El Cliente"
        )

    def test_legacy_report_new_version_is_unaffected(self, client, session):
        """Historia B9's TestLegacyCreationUnaffected equivalent for
        new_version: a legacy report's content-only save must behave exactly
        as before this fix — no schema_version/rendering_snapshot appears."""
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
        report_id = create_resp.json()["id"]

        new_version_resp = client.post(
            f"/api/v1/reports/{report_id}/new_version",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "report": _minimal_report_content("Actualizado"),
            },
            headers=headers,
        )
        assert new_version_resp.status_code == 200, new_version_resp.text
        assert new_version_resp.json()["schema_version"] is None
        assert new_version_resp.json()["template_version_id"] is None

        reread = client.get(f"/api/v1/reports/{report_id}", headers=headers).json()
        assert reread["schema_version"] is None
        assert "rendering_snapshot" not in reread["report"]
