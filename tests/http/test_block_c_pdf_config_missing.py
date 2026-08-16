"""Céluma 1.3, Phase 5 Block C — official-PDF behaviour when
`PDF_GENERATOR_BASE_URL` is absent (Block A finding A-004).

A-004 records that the setting is required, has no default by design, and is
set by no deployed environment. Block C §C5 must therefore validate what
happens *without* it — and that behaviour had no test: every existing case in
`test_report_pdf_generation.py` stubs `_render_pdf` away, so the
configuration guard at the top of the real renderer was never executed.

These tests deliberately do **not** use `stub_pdf_render`. They run the real
`ReportPdfGenerationService._render_pdf` with `settings.pdf_generator_base_url`
cleared, which raises `CONFIG_MISSING` before Playwright is imported or any
browser is launched — so they stay fast and hermetic while covering the exact
production failure A-004 predicts.

What is asserted is the whole "no silent fallback" contract: the request
fails loudly, the version is durably FAILED with the real error code, no
artifact is invented, and the report's own lifecycle state is untouched.
"""
import pytest
from sqlmodel import Session, select

from app.core.config import settings
from app.models.enums import ReportStatus
from app.models.report import Report, ReportVersion
from app.models.storage import StorageObject

from .conftest import FakeS3Service, make_pdf_bytes
from .factories import auth_headers, create_branch, create_order, create_tenant, create_user


def _generate_url(report_id, version_no=1) -> str:
    return f"/api/v1/reports/{report_id}/versions/{version_no}/generate-pdf"


@pytest.fixture(name="unconfigured_generator")
def unconfigured_generator_fixture(monkeypatch):
    """The A-004 environment: the setting is simply not there."""
    monkeypatch.setattr(settings, "pdf_generator_base_url", None)


@pytest.fixture(name="approved_report")
def approved_report_fixture(session: Session):
    tenant = create_tenant(session)
    branch = create_branch(session, tenant)
    order = create_order(session, tenant, branch)
    user = create_user(session, tenant, email="editor@a004.example")
    report = Report(
        tenant_id=tenant.id,
        branch_id=branch.id,
        order_id=order.id,
        status=ReportStatus.APPROVED,
    )
    session.add(report)
    session.flush()
    version = ReportVersion(report_id=report.id, version_no=1, is_current=True)
    session.add(version)
    session.commit()
    session.refresh(report)
    session.refresh(version)
    return {"tenant": tenant, "user": user, "report": report, "version": version}


class TestMissingPdfGeneratorConfiguration:
    def test_generation_fails_loudly_with_the_real_cause(
        self, client, session: Session, approved_report, unconfigured_generator
    ):
        response = client.post(
            _generate_url(approved_report["report"].id),
            headers=auth_headers(approved_report["user"]),
        )

        assert response.status_code == 422, response.text
        assert "PDF_GENERATOR_BASE_URL" in response.json()["detail"]

    def test_the_version_lands_in_failed_with_config_missing(
        self, client, session: Session, approved_report, unconfigured_generator
    ):
        client.post(
            _generate_url(approved_report["report"].id),
            headers=auth_headers(approved_report["user"]),
        )

        session.expire_all()
        version = session.get(ReportVersion, approved_report["version"].id)
        # Never left stuck in GENERATING — the failure is durable and named.
        assert version.pdf_generation_status == "FAILED"
        assert version.pdf_error_code == "CONFIG_MISSING"

    def test_no_artifact_is_invented_and_nothing_reaches_storage(
        self, client, session: Session, approved_report, unconfigured_generator
    ):
        client.post(
            _generate_url(approved_report["report"].id),
            headers=auth_headers(approved_report["user"]),
        )

        session.expire_all()
        version = session.get(ReportVersion, approved_report["version"].id)
        assert version.pdf_storage_id is None
        assert version.pdf_sha256 is None
        assert version.pdf_size_bytes is None
        assert version.pdf_page_count is None
        assert version.pdf_generated_at is None
        # No silent fallback to a placeholder document, in the database or
        # in the object store.
        assert (
            session.exec(
                select(StorageObject).where(StorageObject.tenant_id == approved_report["tenant"].id)
            ).all()
            == []
        )
        assert FakeS3Service.store == {}

    def test_the_report_lifecycle_state_is_not_mutated_by_the_failure(
        self, client, session: Session, approved_report, unconfigured_generator
    ):
        client.post(
            _generate_url(approved_report["report"].id),
            headers=auth_headers(approved_report["user"]),
        )

        session.expire_all()
        report = session.get(Report, approved_report["report"].id)
        assert report.status == ReportStatus.APPROVED
        assert report.published_at is None

    def test_a_retry_after_configuration_arrives_succeeds(
        self, client, session: Session, approved_report, unconfigured_generator, monkeypatch
    ):
        """FAILED is retryable — the missing setting must not poison the
        version permanently.

        The first call runs the *real* renderer and trips the config guard.
        Only then is the environment repaired (a base URL plus a stand-in for
        the renderer A-004's resolution would provide), so this test covers
        the transition A-004 is actually blocking on rather than a
        pre-stubbed happy path.
        """
        first = client.post(
            _generate_url(approved_report["report"].id),
            headers=auth_headers(approved_report["user"]),
        )
        assert first.status_code == 422

        monkeypatch.setattr(settings, "pdf_generator_base_url", "http://renderer.test")
        monkeypatch.setattr(
            "app.services.report_pdf_generation.ReportPdfGenerationService._render_pdf",
            lambda self, report, version: make_pdf_bytes(1),
        )

        second = client.post(
            _generate_url(approved_report["report"].id),
            headers=auth_headers(approved_report["user"]),
        )
        assert second.status_code == 200, second.text
        assert second.json()["pdf_generation_status"] == "READY"

        session.expire_all()
        version = session.get(ReportVersion, approved_report["version"].id)
        assert version.pdf_error_code is None
        assert version.pdf_storage_id is not None
