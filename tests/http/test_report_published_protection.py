"""HTTP integration tests for protecting published/retracted reports
(Céluma 1.3, Phase 2, Block B, Story B9/B10)."""
import io

import pytest
from sqlmodel import Session

from app.models.enums import ReportStatus
from app.models.report import Report, ReportTemplate, ReportVersion
from app.models.report_template_version import ReportTemplateVersion

from .factories import (
    auth_headers,
    create_branch,
    create_order,
    create_tenant,
    create_user,
    valid_rendering_snapshot,
)


def _create_template_version(session: Session, tenant) -> ReportTemplateVersion:
    template = ReportTemplate(tenant_id=tenant.id, name="Default", template_json={}, is_active=True)
    session.add(template)
    session.flush()
    version = ReportTemplateVersion(
        tenant_id=tenant.id,
        report_template_id=template.id,
        version_number=1,
        configuration=valid_rendering_snapshot(),
    )
    session.add(version)
    session.commit()
    session.refresh(version)
    return version


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


@pytest.mark.parametrize("status", [ReportStatus.PUBLISHED, ReportStatus.RETRACTED])
class TestNewVersionBlockedOnImmutableStatuses:
    def test_new_version_is_rejected(self, client, session, status):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")
        report, _ = _create_report_at_status(session, tenant, branch, order, status)

        resp = client.post(
            f"/api/v1/reports/{report.id}/new_version",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "report": {"base": {}, "sections": {}},
            },
            headers=auth_headers(user),
        )
        assert resp.status_code == 409

    def test_pdf_upload_to_latest_version_is_rejected(self, client, session, status):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")
        report, _ = _create_report_at_status(session, tenant, branch, order, status)

        resp = client.post(
            f"/api/v1/reports/{report.id}/pdf",
            files={"file": ("report.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")},
            headers=auth_headers(user),
        )
        assert resp.status_code == 409

    def test_pdf_upload_to_specific_version_is_rejected(self, client, session, status):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")
        report, version = _create_report_at_status(session, tenant, branch, order, status)

        resp = client.post(
            f"/api/v1/reports/{report.id}/versions/{version.version_no}/pdf",
            files={"file": ("report.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")},
            headers=auth_headers(user),
        )
        assert resp.status_code == 409


@pytest.mark.parametrize(
    "status",
    # Céluma 1.3.1 Block B (finding B-3): APPROVED was in this list until the
    # reopen route existed. It moved to the rejected set below — see
    # `TestNewVersionBlockedAfterApproval` and
    # `app/services/report_authorization.py::CONTENT_EDITABLE_STATUSES`.
    # DRAFT and IN_REVIEW are unchanged: nothing has been approved in either,
    # so an edit bypasses no decision.
    [ReportStatus.DRAFT, ReportStatus.IN_REVIEW],
)
class TestNewVersionAllowedOnEditableStatuses:
    def test_new_version_succeeds(self, client, session, status):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")
        report, _ = _create_report_at_status(session, tenant, branch, order, status)

        resp = client.post(
            f"/api/v1/reports/{report.id}/new_version",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "report": {"base": {}, "sections": {}},
            },
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text


class TestNewVersionBlockedAfterApproval:
    """Céluma 1.3.1 Block B, finding B-3.

    Story B9 (Phase 2, Block B) froze content for PUBLISHED and RETRACTED and
    left APPROVED "fully editable", which was coherent while approval was the
    last step before signing and there was no way back from it. Block B added
    `POST /{id}/reopen`, and an editable APPROVED then became a hole straight
    through the lifecycle it had just established.
    """

    def test_new_version_is_rejected_on_an_approved_report(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")
        report, _ = _create_report_at_status(
            session, tenant, branch, order, ReportStatus.APPROVED
        )

        resp = client.post(
            f"/api/v1/reports/{report.id}/new_version",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "report": {"base": {}, "sections": {}},
            },
            headers=auth_headers(user),
        )
        assert resp.status_code == 409, resp.text

    def test_the_rejection_points_at_the_reopen_route(self, client, session):
        """The author is told what to do next, not merely refused — otherwise
        the lifecycle looks broken rather than deliberate."""
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="admin@t1.example")
        report, _ = _create_report_at_status(
            session, tenant, branch, order, ReportStatus.APPROVED
        )

        resp = client.post(
            f"/api/v1/reports/{report.id}/new_version",
            json={
                "tenant_id": str(tenant.id),
                "branch_id": str(branch.id),
                "order_id": str(order.id),
                "report": {"base": {}, "sections": {}},
            },
            headers=auth_headers(user),
        )
        assert resp.status_code == 409
        assert "reábrelo" in resp.json()["detail"].lower()
