"""HTTP integration tests for official PDF generation (Céluma 1.3, Phase 2,
Block E, Historias E4-E9).

Real Chromium never runs here — `stub_pdf_render` (conftest.py) replaces
ReportPdfGenerationService._render_pdf so these tests are fast and
deterministic, while the actual validation/hash/page-count/persistence logic
(E5, E6, E9) still runs for real against the bytes the stub returns.
"""
from datetime import datetime, timedelta

import pytest
from sqlmodel import Session

from app.core.config import settings
from app.models.enums import ReportStatus
from app.models.report import Report, ReportVersion
from app.models.storage import StorageObject

from .conftest import FakeS3Service, make_pdf_bytes
from .factories import auth_headers, create_branch, create_order, create_tenant, create_user


def _create_report_at_status(
    session: Session, tenant, branch, order, status: ReportStatus
) -> tuple[Report, ReportVersion]:
    report = Report(tenant_id=tenant.id, branch_id=branch.id, order_id=order.id, status=status)
    session.add(report)
    session.flush()
    version = ReportVersion(report_id=report.id, version_no=1, is_current=True)
    session.add(version)
    session.commit()
    session.refresh(report)
    session.refresh(version)
    return report, version


def _generate_url(report_id, version_no=1) -> str:
    return f"/api/v1/reports/{report_id}/versions/{version_no}/generate-pdf"


class TestGenerationSuccess:
    def test_generates_ready_artifact_with_hash_and_page_count(self, client, session, stub_pdf_render):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="editor@t1.example")
        report, version = _create_report_at_status(session, tenant, branch, order, ReportStatus.APPROVED)

        stub_pdf_render.succeed(make_pdf_bytes(2))
        resp = client.post(_generate_url(report.id), headers=auth_headers(user))

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["pdf_generation_status"] == "READY"
        assert body["pdf_page_count"] == 2
        assert body["pdf_size_bytes"] > 0
        assert len(body["pdf_sha256"]) == 64
        assert stub_pdf_render.call_count == 1

        session.refresh(version)
        assert version.pdf_storage_id is not None
        storage = session.get(StorageObject, version.pdf_storage_id)
        assert storage.object_key in FakeS3Service.store
        assert storage.object_key.endswith(".pdf")
        assert "/official/" in storage.object_key

    def test_is_idempotent_once_ready(self, client, session, stub_pdf_render):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="editor@t1.example")
        report, _ = _create_report_at_status(session, tenant, branch, order, ReportStatus.APPROVED)

        stub_pdf_render.succeed(make_pdf_bytes(1))
        first = client.post(_generate_url(report.id), headers=auth_headers(user))
        second = client.post(_generate_url(report.id), headers=auth_headers(user))

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["pdf_sha256"] == second.json()["pdf_sha256"]
        # The second call must not have re-invoked the (expensive) render step.
        assert stub_pdf_render.call_count == 1


class TestGenerationRetry:
    def test_retries_after_a_failed_attempt(self, client, session, stub_pdf_render):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="editor@t1.example")
        report, _ = _create_report_at_status(session, tenant, branch, order, ReportStatus.APPROVED)

        stub_pdf_render.fail(RuntimeError("simulated headless browser crash"))
        failed = client.post(_generate_url(report.id), headers=auth_headers(user))
        assert failed.status_code == 422
        assert failed.json()["detail"]

        stub_pdf_render.succeed(make_pdf_bytes(1))
        retried = client.post(_generate_url(report.id), headers=auth_headers(user))
        assert retried.status_code == 200
        body = retried.json()
        assert body["pdf_generation_status"] == "READY"
        assert body["pdf_error_code"] is None
        assert body["pdf_error_message"] is None

    def test_orphaned_generating_state_is_retried_after_timeout(self, client, session, stub_pdf_render):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="editor@t1.example")
        report, version = _create_report_at_status(session, tenant, branch, order, ReportStatus.APPROVED)

        # Simulate a crash mid-generation: GENERATING with a start time far
        # enough in the past to count as orphaned/stale.
        version.pdf_generation_status = "GENERATING"
        stale_seconds = settings.pdf_generation_timeout_seconds * 4
        version.pdf_generation_started_at = datetime.utcnow() - timedelta(seconds=stale_seconds)
        session.add(version)
        session.commit()

        stub_pdf_render.succeed(make_pdf_bytes(1))
        resp = client.post(_generate_url(report.id), headers=auth_headers(user))
        assert resp.status_code == 200, resp.text
        assert resp.json()["pdf_generation_status"] == "READY"

    def test_concurrent_generation_in_progress_is_rejected(self, client, session, stub_pdf_render):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="editor@t1.example")
        report, version = _create_report_at_status(session, tenant, branch, order, ReportStatus.APPROVED)

        version.pdf_generation_status = "GENERATING"
        version.pdf_generation_started_at = datetime.utcnow()
        session.add(version)
        session.commit()

        resp = client.post(_generate_url(report.id), headers=auth_headers(user))
        assert resp.status_code == 409
        assert stub_pdf_render.call_count == 0


class TestGenerationValidation:
    def test_rejects_bytes_without_pdf_header(self, client, session, stub_pdf_render):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="editor@t1.example")
        report, version = _create_report_at_status(session, tenant, branch, order, ReportStatus.APPROVED)

        stub_pdf_render.succeed(b"not a pdf at all")
        resp = client.post(_generate_url(report.id), headers=auth_headers(user))
        assert resp.status_code == 422

        session.refresh(version)
        assert version.pdf_generation_status == "FAILED"
        assert version.pdf_error_code == "INVALID_PDF_HEADER"
        assert version.pdf_storage_id is None

    def test_rejects_empty_bytes(self, client, session, stub_pdf_render):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="editor@t1.example")
        report, version = _create_report_at_status(session, tenant, branch, order, ReportStatus.APPROVED)

        stub_pdf_render.succeed(b"")
        resp = client.post(_generate_url(report.id), headers=auth_headers(user))
        assert resp.status_code == 422
        session.refresh(version)
        assert version.pdf_error_code == "EMPTY_PDF"

    def test_rejects_oversized_pdf(self, client, session, stub_pdf_render, monkeypatch):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="editor@t1.example")
        report, version = _create_report_at_status(session, tenant, branch, order, ReportStatus.APPROVED)

        monkeypatch.setattr(settings, "pdf_max_size_bytes", 10)
        stub_pdf_render.succeed(make_pdf_bytes(1))
        resp = client.post(_generate_url(report.id), headers=auth_headers(user))
        assert resp.status_code == 422
        session.refresh(version)
        assert version.pdf_error_code == "PDF_TOO_LARGE"

    def test_rejects_too_many_pages(self, client, session, stub_pdf_render, monkeypatch):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="editor@t1.example")
        report, version = _create_report_at_status(session, tenant, branch, order, ReportStatus.APPROVED)

        monkeypatch.setattr(settings, "pdf_max_page_count", 1)
        stub_pdf_render.succeed(make_pdf_bytes(3))
        resp = client.post(_generate_url(report.id), headers=auth_headers(user))
        assert resp.status_code == 422
        session.refresh(version)
        assert version.pdf_error_code == "PDF_TOO_MANY_PAGES"


@pytest.mark.parametrize("status", [ReportStatus.PUBLISHED, ReportStatus.RETRACTED])
class TestGenerationImmutability:
    def test_generation_is_rejected_for_immutable_reports(self, client, session, stub_pdf_render, status):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="editor@t1.example")
        report, _ = _create_report_at_status(session, tenant, branch, order, status)

        resp = client.post(_generate_url(report.id), headers=auth_headers(user))
        assert resp.status_code == 409
        assert stub_pdf_render.call_count == 0

    def test_generation_is_rejected_even_when_already_ready(self, client, session, stub_pdf_render, status):
        """"Reject if PUBLISHED with ready PDF" — regeneration must be
        refused outright, not silently no-op'd, once a report is immutable."""
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="editor@t1.example")
        report, version = _create_report_at_status(session, tenant, branch, order, ReportStatus.APPROVED)

        stub_pdf_render.succeed(make_pdf_bytes(1))
        ready = client.post(_generate_url(report.id), headers=auth_headers(user))
        assert ready.status_code == 200
        sha_before = ready.json()["pdf_sha256"]

        report.status = status
        session.add(report)
        session.commit()

        resp = client.post(_generate_url(report.id), headers=auth_headers(user))
        assert resp.status_code == 409
        session.refresh(version)
        assert version.pdf_sha256 == sha_before


class TestGenerationPermissionsAndIsolation:
    def test_requires_reports_edit_permission(self, client, session, stub_pdf_render):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="viewer@t1.example", roles=("viewer",))
        report, _ = _create_report_at_status(session, tenant, branch, order, ReportStatus.APPROVED)

        resp = client.post(_generate_url(report.id), headers=auth_headers(user))
        assert resp.status_code == 403
        assert stub_pdf_render.call_count == 0

    def test_cross_tenant_report_is_not_found(self, client, session, stub_pdf_render):
        tenant_a = create_tenant(session, name="Tenant A")
        branch_a = create_branch(session, tenant_a)
        order_a = create_order(session, tenant_a, branch_a)
        report, _ = _create_report_at_status(session, tenant_a, branch_a, order_a, ReportStatus.APPROVED)

        tenant_b = create_tenant(session, name="Tenant B")
        user_b = create_user(session, tenant_b, email="editor@t2.example")

        resp = client.post(_generate_url(report.id), headers=auth_headers(user_b))
        assert resp.status_code == 404
        assert stub_pdf_render.call_count == 0

    def test_unknown_version_is_not_found(self, client, session, stub_pdf_render):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="editor@t1.example")
        report, _ = _create_report_at_status(session, tenant, branch, order, ReportStatus.APPROVED)

        resp = client.post(_generate_url(report.id, version_no=99), headers=auth_headers(user))
        assert resp.status_code == 404


class TestManualUploadResetsGenerationMetadata:
    """The legacy manual-upload endpoints bypass ReportPdfGenerationService
    entirely — any generation metadata a prior official generation left
    behind must be cleared so it never keeps claiming READY for bytes that
    were never validated (see report_pdf_generation.py's module docstring
    and the reset added directly in upload_pdf_to_specific_version /
    upload_pdf_to_latest_version)."""

    def test_uploading_after_a_ready_generation_clears_pdf_metadata(
        self, client, session, stub_pdf_render
    ):
        import io

        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="editor@t1.example")
        report, version = _create_report_at_status(session, tenant, branch, order, ReportStatus.APPROVED)

        stub_pdf_render.succeed(make_pdf_bytes(1))
        ready = client.post(_generate_url(report.id), headers=auth_headers(user))
        assert ready.status_code == 200

        upload = client.post(
            f"/api/v1/reports/{report.id}/versions/{version.version_no}/pdf",
            files={"file": ("manual.pdf", io.BytesIO(b"%PDF-1.4 manual override"), "application/pdf")},
            headers=auth_headers(user),
        )
        assert upload.status_code == 200, upload.text

        session.refresh(version)
        assert version.pdf_generation_status is None
        assert version.pdf_sha256 is None
        assert version.pdf_size_bytes is None
        assert version.pdf_page_count is None
        assert version.pdf_storage_id is not None  # the manual upload itself still succeeded
