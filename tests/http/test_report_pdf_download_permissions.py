"""PDF download authorization matrix — post-Fase-2 remediation, R14/R15.

Confirms the real root cause found while reproducing bug 4: the backend
was already correct (both GET .../pdf endpoints require only
`reports:read`), so every seeded role except a user with literally no
report permissions can download. The two real fixes were (a) the
frontend gating the download BUTTON on `reports:sign` instead of
`reports:read` (see report_editor.tsx), and (b) a 403-vs-404
inconsistency on cross-tenant access between the two GET endpoints (see
reports.py `get_pdf_of_specific_version`) — both covered here at the
HTTP layer, which is what the frontend gate cannot fix by itself.
"""
from app.core.rbac import ROLE_ASSISTANT, ROLE_BILLING, ROLE_LAB_TECH, ROLE_PATHOLOGIST, ROLE_REVIEWER, ROLE_SUPERUSER, ROLE_VIEWER
from app.models.enums import ReportStatus
from app.models.report import Report, ReportVersion

from .conftest import make_pdf_bytes
from .factories import auth_headers, create_branch, create_order, create_tenant, create_user


def _create_report_with_pdf(client, session, tenant, branch, order, stub_pdf_render, editor):
    report = Report(tenant_id=tenant.id, branch_id=branch.id, order_id=order.id, status=ReportStatus.APPROVED)
    session.add(report)
    session.flush()
    version = ReportVersion(report_id=report.id, version_no=1, is_current=True)
    session.add(version)
    session.commit()
    session.refresh(report)

    stub_pdf_render.succeed(make_pdf_bytes(1))
    resp = client.post(
        f"/api/v1/reports/{report.id}/versions/1/generate-pdf", headers=auth_headers(editor)
    )
    assert resp.status_code == 200, resp.text
    return report


class TestDownloadPermissionMatrix:
    def test_superuser_can_download(self, client, session, stub_pdf_render):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        editor = create_user(session, tenant, email="editor@t1.example", roles=(ROLE_SUPERUSER,))
        report = _create_report_with_pdf(client, session, tenant, branch, order, stub_pdf_render, editor)

        resp = client.get(f"/api/v1/reports/{report.id}/versions/1/pdf", headers=auth_headers(editor))
        assert resp.status_code == 200

    def test_pathologist_editor_can_download_without_sign(self, client, session, stub_pdf_render):
        """reports:edit holders (pathologist) can generate AND download —
        download does not additionally require reports:sign."""
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        editor = create_user(session, tenant, email="editor@t1.example", roles=(ROLE_PATHOLOGIST,))
        report = _create_report_with_pdf(client, session, tenant, branch, order, stub_pdf_render, editor)

        resp = client.get(f"/api/v1/reports/{report.id}/versions/1/pdf", headers=auth_headers(editor))
        assert resp.status_code == 200

    def test_reviewer_can_download(self, client, session, stub_pdf_render):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        editor = create_user(session, tenant, email="editor@t1.example", roles=(ROLE_PATHOLOGIST,))
        reviewer = create_user(session, tenant, email="rev@t1.example", roles=(ROLE_REVIEWER,))
        report = _create_report_with_pdf(client, session, tenant, branch, order, stub_pdf_render, editor)

        resp = client.get(f"/api/v1/reports/{report.id}/versions/1/pdf", headers=auth_headers(reviewer))
        assert resp.status_code == 200

    def test_read_only_viewer_can_download(self, client, session, stub_pdf_render):
        """The exact scenario the bug report describes: a user who can
        read/view reports but has no edit/sign permission must still be
        able to download the official PDF — reports:read is sufficient."""
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        editor = create_user(session, tenant, email="editor@t1.example", roles=(ROLE_PATHOLOGIST,))
        viewer = create_user(session, tenant, email="viewer@t1.example", roles=(ROLE_VIEWER,))
        report = _create_report_with_pdf(client, session, tenant, branch, order, stub_pdf_render, editor)

        resp = client.get(f"/api/v1/reports/{report.id}/versions/1/pdf", headers=auth_headers(viewer))
        assert resp.status_code == 200

    def test_lab_tech_and_assistant_and_billing_can_download(self, client, session, stub_pdf_render):
        """Every seeded role holds reports:read (v1_0_0_initial_schema.py)
        — download must not be more restrictive than that."""
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        editor = create_user(session, tenant, email="editor@t1.example", roles=(ROLE_PATHOLOGIST,))
        report = _create_report_with_pdf(client, session, tenant, branch, order, stub_pdf_render, editor)

        for role in (ROLE_LAB_TECH, ROLE_ASSISTANT, ROLE_BILLING):
            user = create_user(session, tenant, email=f"{role}@t1.example", roles=(role,))
            resp = client.get(f"/api/v1/reports/{report.id}/versions/1/pdf", headers=auth_headers(user))
            assert resp.status_code == 200, f"role={role} got {resp.status_code}"

    def test_user_with_no_report_permissions_is_forbidden(self, client, session, stub_pdf_render):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        editor = create_user(session, tenant, email="editor@t1.example", roles=(ROLE_PATHOLOGIST,))
        report = _create_report_with_pdf(client, session, tenant, branch, order, stub_pdf_render, editor)

        no_perms_user = create_user(session, tenant, email="noperm@t1.example", roles=())
        resp = client.get(f"/api/v1/reports/{report.id}/versions/1/pdf", headers=auth_headers(no_perms_user))
        assert resp.status_code == 403

    def test_cross_tenant_is_not_found_not_forbidden(self, client, session, stub_pdf_render):
        """Post-Fase-2 remediation: both GET pdf endpoints now consistently
        404 on tenant mismatch (previously the specific-version endpoint
        alone leaked existence via a 403)."""
        tenant_a = create_tenant(session, name="Tenant A")
        branch_a = create_branch(session, tenant_a)
        order_a = create_order(session, tenant_a, branch_a)
        editor_a = create_user(session, tenant_a, email="editor@a.example", roles=(ROLE_PATHOLOGIST,))
        report = _create_report_with_pdf(client, session, tenant_a, branch_a, order_a, stub_pdf_render, editor_a)

        tenant_b = create_tenant(session, name="Tenant B")
        user_b = create_user(session, tenant_b, email="user@b.example", roles=(ROLE_SUPERUSER,))

        specific = client.get(f"/api/v1/reports/{report.id}/versions/1/pdf", headers=auth_headers(user_b))
        latest = client.get(f"/api/v1/reports/{report.id}/pdf", headers=auth_headers(user_b))
        assert specific.status_code == 404
        assert latest.status_code == 404

    def test_payment_locked_order_does_not_block_internal_download(
        self, client, session, stub_pdf_render
    ):
        """Quinta remediación post-Fase 2 — INVERSIÓN DELIBERADA de esta
        prueba.

        Antes afirmaba `403`. Esa aserción es justamente lo que dejó pasar el
        bug real: `Order.billed_lock` es la compuerta de entrega a TERCEROS
        (paciente y médico solicitante, `app/api/v1/portal.py`), y se había
        colado también en los endpoints internos del PDF, donde `/full` —
        mismo permiso, mismo reporte, contenido clínico completo — nunca la
        aplicó. El resultado en producción: la patóloga firmaba su reporte y
        recibía `{"detail":"Report access blocked due to pending payment"}`
        al intentar descargarlo.

        La prueba se conserva (no se borra) con la afirmación correcta: el
        personal interno descarga, y `portal.py` sigue bloqueando. Ver
        official-pdf-download-root-cause.md.
        """
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        editor = create_user(session, tenant, email="editor@t1.example", roles=(ROLE_SUPERUSER,))
        report = _create_report_with_pdf(client, session, tenant, branch, order, stub_pdf_render, editor)

        order.billed_lock = True
        session.add(order)
        session.commit()

        specific = client.get(
            f"/api/v1/reports/{report.id}/versions/1/pdf", headers=auth_headers(editor)
        )
        latest = client.get(f"/api/v1/reports/{report.id}/pdf", headers=auth_headers(editor))
        full = client.get(f"/api/v1/reports/{report.id}/full", headers=auth_headers(editor))

        assert full.status_code == 200
        assert specific.status_code == 200, specific.text
        assert latest.status_code == 200, latest.text
