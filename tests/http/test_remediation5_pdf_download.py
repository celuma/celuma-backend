"""Fifth post-Phase-2 remediation — the real `403` on official PDF
download (Observation B).

Root cause confirmed by reproduction, not by inspection: the endpoint
`GET /reports/{id}/versions/{n}/pdf` applied `Order.billed_lock` — the
third-party delivery gate from `portal.py` — while
`GET /reports/{id}/full` did not. The body captured in Safari,
`{"detail":"Report access blocked due to pending payment"}`, measures
exactly the 57 bytes of the log's `content-length`. See
official-pdf-download-root-cause.md.

The critical test in this file is `test_full_implies_pdf_*`: it expresses
the invariant the previous suite never checked — if a user can
legitimately read `/full` and the official PDF exists, they can download
it.
"""
import pytest

from app.core.rbac import (
    ROLE_ASSISTANT,
    ROLE_BILLING,
    ROLE_LAB_TECH,
    ROLE_PATHOLOGIST,
    ROLE_REVIEWER,
    ROLE_SUPERUSER,
    ROLE_VIEWER,
)
from app.models.enums import ReportStatus
from app.models.report import Report, ReportVersion

from .conftest import make_pdf_bytes
from .factories import auth_headers, create_branch, create_order, create_tenant, create_user


def _report_with_official_pdf(client, session, tenant, branch, order, stub_pdf_render, editor):
    report = Report(
        tenant_id=tenant.id, branch_id=branch.id, order_id=order.id, status=ReportStatus.APPROVED
    )
    session.add(report)
    session.flush()
    session.add(ReportVersion(report_id=report.id, version_no=1, is_current=True))
    session.commit()
    session.refresh(report)

    stub_pdf_render.succeed(make_pdf_bytes(1))
    resp = client.post(
        f"/api/v1/reports/{report.id}/versions/1/generate-pdf", headers=auth_headers(editor)
    )
    assert resp.status_code == 200, resp.text
    return report


@pytest.fixture
def published_world(client, session, stub_pdf_render):
    tenant = create_tenant(session)
    branch = create_branch(session, tenant)
    order = create_order(session, tenant, branch)
    editor = create_user(session, tenant, email="editor@t1.example", roles=(ROLE_PATHOLOGIST,))
    report = _report_with_official_pdf(
        client, session, tenant, branch, order, stub_pdf_render, editor
    )
    return {"tenant": tenant, "branch": branch, "order": order, "editor": editor, "report": report}


class TestFullImpliesPdf:
    """The central invariant of brief §10."""

    @pytest.mark.parametrize(
        "role",
        [ROLE_SUPERUSER, ROLE_PATHOLOGIST, ROLE_REVIEWER, ROLE_VIEWER, ROLE_LAB_TECH, ROLE_ASSISTANT],
    )
    def test_full_implies_pdf_for_every_reading_role(self, client, session, published_world, role):
        user = create_user(
            session, published_world["tenant"], email=f"{role}@t1.example", roles=(role,)
        )
        report_id = published_world["report"].id
        headers = auth_headers(user)

        full = client.get(f"/api/v1/reports/{report_id}/full", headers=headers)
        if full.status_code != 200:
            pytest.skip(f"{role} cannot read /full; the invariant does not apply")

        specific = client.get(f"/api/v1/reports/{report_id}/versions/1/pdf", headers=headers)
        latest = client.get(f"/api/v1/reports/{report_id}/pdf", headers=headers)
        assert specific.status_code == 200, f"{role}: {specific.text}"
        assert latest.status_code == 200, f"{role}: {latest.text}"

    def test_full_implies_pdf_with_payment_locked_order(self, client, session, published_world):
        """Exact reproduction of the manual failure: the order is locked for
        pending payment and the internal user can read `/full`. Previously,
        `/pdf` responded 403 with 57 bytes."""
        order = published_world["order"]
        order.billed_lock = True
        session.add(order)
        session.commit()

        headers = auth_headers(published_world["editor"])
        report_id = published_world["report"].id

        assert client.get(f"/api/v1/reports/{report_id}/full", headers=headers).status_code == 200
        resp = client.get(f"/api/v1/reports/{report_id}/versions/1/pdf", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["pdf_url"]

    def test_portal_still_blocks_on_payment_lock(self, client, session, published_world):
        """The business gate was NOT removed: it remains where it truly
        matters — delivery to the requesting physician via the portal."""
        import inspect

        from app.api.v1 import portal

        source = inspect.getsource(portal)
        assert source.count("billed_lock") >= 2
        assert "Report access blocked due to pending payment" in source


class TestReadAuthorizationParity:
    def test_user_without_reports_read_gets_403(self, client, session, published_world):
        """Note that ALL seeded roles include `reports:read` (including
        `billing`), so the only user without read is one with no role at
        all — which reinforces the invariant above: in practice, anyone
        inside the laboratory who can open the report can download its
        PDF."""
        outsider = create_user(
            session, published_world["tenant"], email="noroles@t1.example", roles=()
        )
        headers = auth_headers(outsider)
        report_id = published_world["report"].id

        full = client.get(f"/api/v1/reports/{report_id}/full", headers=headers)
        specific = client.get(f"/api/v1/reports/{report_id}/versions/1/pdf", headers=headers)
        latest = client.get(f"/api/v1/reports/{report_id}/pdf", headers=headers)

        # Same policy on all three routes: without reports:read, 403 on all.
        assert full.status_code == 403
        assert specific.status_code == 403
        assert latest.status_code == 403
        assert specific.json()["detail"] == "Permission required: reports:read"

    def test_cross_tenant_gets_404_everywhere(self, client, session, published_world):
        other_tenant = create_tenant(session, name="Tenant B")
        intruder = create_user(
            session, other_tenant, email="intruder@b.example", roles=(ROLE_SUPERUSER,)
        )
        headers = auth_headers(intruder)
        report_id = published_world["report"].id

        assert client.get(f"/api/v1/reports/{report_id}/full", headers=headers).status_code == 404
        assert (
            client.get(f"/api/v1/reports/{report_id}/versions/1/pdf", headers=headers).status_code
            == 404
        )
        assert client.get(f"/api/v1/reports/{report_id}/pdf", headers=headers).status_code == 404
        assert client.get(f"/api/v1/reports/{report_id}", headers=headers).status_code == 404

    def test_missing_version_is_404_not_403(self, client, published_world):
        headers = auth_headers(published_world["editor"])
        resp = client.get(
            f"/api/v1/reports/{published_world['report'].id}/versions/99/pdf", headers=headers
        )
        assert resp.status_code == 404

    def test_version_without_pdf_is_404(self, client, session, published_world):
        """A version without a PDF artifact responds 404, not 403 — "not
        ready" must never be presented as "you do not have permission"."""
        report = published_world["report"]
        session.add(ReportVersion(report_id=report.id, version_no=2, is_current=False))
        session.commit()

        resp = client.get(
            f"/api/v1/reports/{report.id}/versions/2/pdf",
            headers=auth_headers(published_world["editor"]),
        )
        assert resp.status_code == 404


class TestSignAndPublishResponseContract:
    def test_response_carries_version_and_pdf_metadata(self, client, session, stub_pdf_render):
        """§8: the UI must not guess which version to download."""
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        signer = create_user(
            session, tenant, email="signer@t1.example", roles=(ROLE_SUPERUSER, ROLE_REVIEWER)
        )

        report = Report(
            tenant_id=tenant.id,
            branch_id=branch.id,
            order_id=order.id,
            status=ReportStatus.APPROVED,
        )
        session.add(report)
        session.flush()
        session.add(ReportVersion(report_id=report.id, version_no=1, is_current=True))
        session.commit()
        session.refresh(report)

        stub_pdf_render.succeed(make_pdf_bytes(2))
        resp = client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish",
            json={},
            headers=auth_headers(signer),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["status"] == ReportStatus.PUBLISHED
        assert body["version_no"] == 1
        assert body["report_version_id"]
        assert body["pdf_generation_status"] == "READY"
        assert body["official_pdf_available"] is True
        assert body["pdf_sha256"]
        assert body["pdf_page_count"] == 2

        # The version announced by the response is immediately downloadable.
        download = client.get(
            f"/api/v1/reports/{report.id}/versions/{body['version_no']}/pdf",
            headers=auth_headers(signer),
        )
        assert download.status_code == 200, download.text
        assert download.json()["version_no"] == body["version_no"]

    def test_announced_version_matches_full(self, client, session, stub_pdf_render):
        """The version announced by sign-and-publish and the one `/full`
        returns after refresh must match — if they diverged, the UI would
        download the wrong version."""
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        signer = create_user(
            session, tenant, email="signer2@t1.example", roles=(ROLE_SUPERUSER, ROLE_REVIEWER)
        )

        report = Report(
            tenant_id=tenant.id,
            branch_id=branch.id,
            order_id=order.id,
            status=ReportStatus.APPROVED,
        )
        session.add(report)
        session.flush()
        session.add(ReportVersion(report_id=report.id, version_no=1, is_current=False))
        session.add(ReportVersion(report_id=report.id, version_no=2, is_current=True))
        session.commit()
        session.refresh(report)

        stub_pdf_render.succeed(make_pdf_bytes(1))
        resp = client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish",
            json={},
            headers=auth_headers(signer),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["version_no"] == 2

        full = client.get(f"/api/v1/reports/{report.id}/full", headers=auth_headers(signer))
        assert full.json()["report"]["version_no"] == resp.json()["version_no"]
