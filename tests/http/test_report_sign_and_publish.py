"""HTTP integration tests for `POST /{report_id}/sign-and-publish` — segunda
remediación post-Fase 2 (UX). Ver signed-pdf-publication-workflow.md.

Cubre: una sola acción produce READY+PUBLISHED sin necesidad de un
`generate-pdf` previo; el claim (`publish_started_at`/`publish_started_by`)
rechaza un segundo intento concurrente con 409 pero recupera un claim
"stale" (huérfano); una generación fallida deja el reporte APPROVED y
reintentable, sin signed_by/signed_at ni PUBLISHED.
"""
from datetime import datetime, timedelta

from sqlmodel import Session

from app.core.config import settings
from app.models.enums import ReportStatus
from app.models.report import Report, ReportVersion
from app.services.report_pdf_generation import ReportPdfGenerationError

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


class TestSingleActionFlow:
    def test_generates_and_publishes_without_a_prior_generate_pdf_call(
        self, client, session, stub_pdf_render
    ):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        reviewer = create_user(session, tenant, email="reviewer@t1.example", roles=("reviewer",))
        report, _ = _create_approved_report(session, tenant, branch, order)

        stub_pdf_render.succeed(make_pdf_bytes(1))
        resp = client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish", json={}, headers=auth_headers(reviewer)
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == ReportStatus.PUBLISHED
        assert body["pdf_generation_status"] == "READY"
        assert body["pdf_sha256"]
        assert stub_pdf_render.call_count == 1

        session.refresh(report)
        assert report.status == ReportStatus.PUBLISHED
        assert report.published_at is not None

    def test_requires_reports_sign_permission(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        no_perm_user = create_user(session, tenant, email="viewer@t1.example", roles=("viewer",))
        report, _ = _create_approved_report(session, tenant, branch, order)

        resp = client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish", json={}, headers=auth_headers(no_perm_user)
        )
        assert resp.status_code == 403

    def test_requires_reviewer_role_even_with_reports_sign_permission_pattern(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        # 'pathologist' has reports:edit but not reports:sign (revoked by
        # v1_1_0_digital_signature.py) — this asserts the permission gate,
        # not the role gate, is what blocks it.
        editor = create_user(session, tenant, email="editor@t1.example", roles=("pathologist",))
        report, _ = _create_approved_report(session, tenant, branch, order)

        resp = client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish", json={}, headers=auth_headers(editor)
        )
        assert resp.status_code == 403

    def test_rejects_when_report_is_not_approved(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        reviewer = create_user(session, tenant, email="reviewer@t1.example", roles=("reviewer",))
        report = Report(tenant_id=tenant.id, branch_id=branch.id, order_id=order.id, status=ReportStatus.DRAFT)
        session.add(report)
        session.commit()

        resp = client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish", json={}, headers=auth_headers(reviewer)
        )
        assert resp.status_code == 400


class TestConcurrencyClaim:
    def test_second_concurrent_call_is_rejected_with_409(self, client, session, stub_pdf_render):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        reviewer = create_user(session, tenant, email="reviewer@t1.example", roles=("reviewer",))
        report, version = _create_approved_report(session, tenant, branch, order)

        # Simulate an in-flight sign-and-publish (fresh claim, not stale).
        version.publish_started_at = datetime.utcnow()
        version.publish_started_by = reviewer.id
        session.add(version)
        session.commit()

        stub_pdf_render.succeed(make_pdf_bytes(1))
        resp = client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish", json={}, headers=auth_headers(reviewer)
        )
        assert resp.status_code == 409
        assert stub_pdf_render.call_count == 0

        session.refresh(report)
        assert report.status == ReportStatus.APPROVED

    def test_stale_claim_is_recovered_and_publish_succeeds(self, client, session, stub_pdf_render):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        reviewer = create_user(session, tenant, email="reviewer@t1.example", roles=("reviewer",))
        report, version = _create_approved_report(session, tenant, branch, order)

        # A crashed prior attempt — claim well past the staleness window.
        stale_after = timedelta(seconds=settings.report_publish_timeout_seconds * 3 + 30)
        version.publish_started_at = datetime.utcnow() - stale_after
        version.publish_started_by = reviewer.id
        session.add(version)
        session.commit()

        stub_pdf_render.succeed(make_pdf_bytes(1))
        resp = client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish", json={}, headers=auth_headers(reviewer)
        )
        assert resp.status_code == 200, resp.text
        session.refresh(report)
        assert report.status == ReportStatus.PUBLISHED


class TestFailedGenerationIsRetryable:
    def test_failed_generation_leaves_report_approved_and_clears_claim(
        self, client, session, stub_pdf_render
    ):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        reviewer = create_user(session, tenant, email="reviewer@t1.example", roles=("reviewer",))
        report, version = _create_approved_report(session, tenant, branch, order)

        stub_pdf_render.fail(ReportPdfGenerationError("RENDER_FAILED", "boom"))
        resp = client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish", json={}, headers=auth_headers(reviewer)
        )
        assert resp.status_code == 422

        session.refresh(report)
        session.refresh(version)
        assert report.status == ReportStatus.APPROVED
        assert version.signed_by is None
        assert version.signed_at is None
        assert version.publish_started_at is None
        assert version.publish_started_by is None

    def test_retry_after_failure_succeeds(self, client, session, stub_pdf_render):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        reviewer = create_user(session, tenant, email="reviewer@t1.example", roles=("reviewer",))
        report, _ = _create_approved_report(session, tenant, branch, order)

        stub_pdf_render.fail(ReportPdfGenerationError("RENDER_FAILED", "boom"))
        first = client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish", json={}, headers=auth_headers(reviewer)
        )
        assert first.status_code == 422

        stub_pdf_render.succeed(make_pdf_bytes(1))
        second = client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish", json={}, headers=auth_headers(reviewer)
        )
        assert second.status_code == 200, second.text
        session.refresh(report)
        assert report.status == ReportStatus.PUBLISHED
