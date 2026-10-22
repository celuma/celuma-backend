"""Independent review R1/R2: a layout failure during official rendering fails signing safely."""
from app.models.enums import ReportStatus
from app.services.report_pdf_generation import ReportPdfGenerationError

from .factories import TEST_USER_PASSWORD, auth_headers, create_branch, create_order, create_tenant
from .test_h0c_official_pdf_signature import _approved_report, _signer


def test_layout_failure_leaves_the_report_approved_and_unpublished(client, session, stub_pdf_render):
    tenant = create_tenant(session)
    branch = create_branch(session, tenant)
    order = create_order(session, tenant, branch)
    signer = _signer(session, tenant, order=order)
    report, _, _ = _approved_report(session, tenant, branch, order)
    stub_pdf_render.fail(ReportPdfGenerationError("LAYOUT_RENDER_FAILED", "layout failed (LAYOUT_OVERFLOW)"))

    resp = client.post(
        f"/api/v1/reports/{report.id}/sign-and-publish",
        json={"password": TEST_USER_PASSWORD},
        headers=auth_headers(signer),
    )
    assert resp.status_code == 422, resp.text
    assert "layout failed" in resp.text
    session.refresh(report)
    assert report.status == ReportStatus.APPROVED
    assert report.published_at is None
