"""HTTP integration tests for the PDF-ready publication gate (Céluma 1.3,
Fase 2, Bloque E, Historia E8): a report can never be signed/published
without a READY official PDF for the version being published.
"""
from sqlmodel import Session

from app.core.rbac import ROLE_REVIEWER
from app.models.enums import ReportStatus
from app.models.report import Report, ReportVersion

from .conftest import make_pdf_bytes
from .factories import auth_headers, create_branch, create_order, create_tenant, create_user


def _create_approved_report(session: Session, tenant, branch, order) -> tuple[Report, ReportVersion]:
    report = Report(tenant_id=tenant.id, branch_id=branch.id, order_id=order.id, status=ReportStatus.APPROVED)
    session.add(report)
    session.flush()
    version = ReportVersion(report_id=report.id, version_no=1, is_current=True)
    session.add(version)
    session.commit()
    session.refresh(report)
    session.refresh(version)
    return report, version


class TestSignRequiresReadyPdf:
    def test_sign_is_rejected_without_a_generated_pdf(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        reviewer = create_user(session, tenant, email="reviewer@t1.example", roles=("reviewer",))
        report, _ = _create_approved_report(session, tenant, branch, order)

        resp = client.post(
            f"/api/v1/reports/{report.id}/sign", json={}, headers=auth_headers(reviewer)
        )
        assert resp.status_code == 422
        session.refresh(report)
        assert report.status == ReportStatus.APPROVED

    def test_sign_is_rejected_while_generation_is_only_in_progress(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        reviewer = create_user(session, tenant, email="reviewer@t1.example", roles=("reviewer",))
        report, version = _create_approved_report(session, tenant, branch, order)
        from datetime import datetime

        version.pdf_generation_status = "GENERATING"
        version.pdf_generation_started_at = datetime.utcnow()
        session.add(version)
        session.commit()

        resp = client.post(
            f"/api/v1/reports/{report.id}/sign", json={}, headers=auth_headers(reviewer)
        )
        assert resp.status_code == 422

    def test_sign_is_rejected_after_a_failed_generation(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        reviewer = create_user(session, tenant, email="reviewer@t1.example", roles=("reviewer",))
        report, version = _create_approved_report(session, tenant, branch, order)
        version.pdf_generation_status = "FAILED"
        version.pdf_error_code = "RENDER_TIMEOUT"
        session.add(version)
        session.commit()

        resp = client.post(
            f"/api/v1/reports/{report.id}/sign", json={}, headers=auth_headers(reviewer)
        )
        assert resp.status_code == 422

    def test_sign_succeeds_once_pdf_is_ready(self, client, session, stub_pdf_render):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        # Realistic handoff: 'pathologist' (has reports:edit) generates the
        # PDF, a separate 'reviewer' (has reports:sign, but NOT reports:edit
        # — see REVIEWER_PERMISSIONS in v1_1_0_digital_signature.py) signs it.
        editor = create_user(session, tenant, email="editor@t1.example", roles=("pathologist",))
        reviewer = create_user(session, tenant, email="reviewer@t1.example", roles=("reviewer",))
        report, _ = _create_approved_report(session, tenant, branch, order)

        stub_pdf_render.succeed(make_pdf_bytes(1))
        gen = client.post(
            f"/api/v1/reports/{report.id}/versions/1/generate-pdf", headers=auth_headers(editor)
        )
        assert gen.status_code == 200

        resp = client.post(
            f"/api/v1/reports/{report.id}/sign", json={}, headers=auth_headers(reviewer)
        )
        assert resp.status_code == 200, resp.text
        session.refresh(report)
        assert report.status == ReportStatus.PUBLISHED

    def test_sign_still_requires_reviewer_role_even_with_ready_pdf(self, client, session, stub_pdf_render):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        # 'superuser' holds every permission (including reports:edit and
        # reports:sign — see factories.py's module docstring) but is a
        # distinct role *code* from 'reviewer'; sign_report's
        # has_any_role({REVIEWER}) role-membership check must still block it
        # even though the permission check alone would pass. ('pathologist'
        # can't be used for this: v1_1_0_digital_signature.py's migration
        # explicitly revokes reports:sign from it.)
        editor = create_user(session, tenant, email="editor@t1.example", roles=("superuser",))
        report, _ = _create_approved_report(session, tenant, branch, order)

        stub_pdf_render.succeed(make_pdf_bytes(1))
        client.post(f"/api/v1/reports/{report.id}/versions/1/generate-pdf", headers=auth_headers(editor))

        resp = client.post(f"/api/v1/reports/{report.id}/sign", json={}, headers=auth_headers(editor))
        assert resp.status_code == 403
        assert ROLE_REVIEWER in resp.text
