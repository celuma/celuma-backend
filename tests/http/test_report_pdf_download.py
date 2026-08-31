"""HTTP integration tests for the official PDF download route (Céluma 1.3,
Phase 2, Block E, Story E10)."""
from sqlmodel import Session

from app.models.enums import ReportStatus
from app.models.report import Report, ReportVersion

from .conftest import make_pdf_bytes
from .factories import auth_headers, create_branch, create_order, create_tenant, create_user


def _create_report(session: Session, tenant, branch, order, status=ReportStatus.APPROVED):
    report = Report(tenant_id=tenant.id, branch_id=branch.id, order_id=order.id, status=status)
    session.add(report)
    session.flush()
    version = ReportVersion(report_id=report.id, version_no=1, is_current=True)
    session.add(version)
    session.commit()
    session.refresh(report)
    session.refresh(version)
    return report, version


class TestOfficialDownload:
    def test_404_when_no_pdf_generated_yet(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="u@t1.example")
        report, _ = _create_report(session, tenant, branch, order)

        resp = client.get(f"/api/v1/reports/{report.id}/versions/1/pdf", headers=auth_headers(user))
        assert resp.status_code == 404

    def test_download_url_uses_order_code_filename_not_patient_data(
        self, client, session, stub_pdf_render
    ):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch, order_code="ORD-XYZ-42")
        user = create_user(session, tenant, email="u@t1.example")
        report, _ = _create_report(session, tenant, branch, order)

        stub_pdf_render.succeed(make_pdf_bytes(1))
        client.post(f"/api/v1/reports/{report.id}/versions/1/generate-pdf", headers=auth_headers(user))

        resp = client.get(f"/api/v1/reports/{report.id}/versions/1/pdf", headers=auth_headers(user))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # H-0c: canonical contract `<ORDER_CODE>-<StudyTypePascalCase>.pdf`,
        # shared with the local copy. This order has no study type, so the
        # study component takes its deterministic fallback. The official name
        # deliberately carries NO version — provenance is the report id,
        # version, object key, sha256 and audit history.
        assert "ORD-XYZ-42-Reporte.pdf" in body["pdf_url"]
        assert "-v1" not in body["pdf_url"].split("filename=")[-1]
        # The point of this test, unchanged: no patient identity in the name.
        for token in ("patient", "paciente", "Doe", "John"):
            assert token not in body["pdf_url"].split("filename=")[-1]

    def test_download_via_latest_version_route_also_works(self, client, session, stub_pdf_render):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        user = create_user(session, tenant, email="u@t1.example")
        report, _ = _create_report(session, tenant, branch, order)

        stub_pdf_render.succeed(make_pdf_bytes(1))
        client.post(f"/api/v1/reports/{report.id}/versions/1/generate-pdf", headers=auth_headers(user))

        resp = client.get(f"/api/v1/reports/{report.id}/pdf", headers=auth_headers(user))
        assert resp.status_code == 200, resp.text
        assert resp.json()["pdf_storage_id"]

    def test_cross_tenant_download_is_not_found(self, client, session, stub_pdf_render):
        tenant_a = create_tenant(session, name="Tenant A")
        branch_a = create_branch(session, tenant_a)
        order_a = create_order(session, tenant_a, branch_a)
        user_a = create_user(session, tenant_a, email="u@t1.example")
        report, _ = _create_report(session, tenant_a, branch_a, order_a)
        stub_pdf_render.succeed(make_pdf_bytes(1))
        client.post(f"/api/v1/reports/{report.id}/versions/1/generate-pdf", headers=auth_headers(user_a))

        tenant_b = create_tenant(session, name="Tenant B")
        user_b = create_user(session, tenant_b, email="u@t2.example")

        resp = client.get(f"/api/v1/reports/{report.id}/versions/1/pdf", headers=auth_headers(user_b))
        # Post-Phase-2 remediation: tightened from `in (403, 404)` now that
        # the tenant-mismatch inconsistency between this endpoint and its
        # "latest version" sibling is fixed — see
        # test_report_pdf_download_permissions.py for the full matrix.
        assert resp.status_code == 404

    def test_published_pdf_survives_and_remains_downloadable(self, client, session, stub_pdf_render):
        """A published report's PDF must stay downloadable — the download
        route only ever reads pdf_storage_id, never regenerates."""
        from app.core.rbac import ROLE_REVIEWER

        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        # 'reviewer' has reports:sign but not reports:edit (see
        # REVIEWER_PERMISSIONS in v1_1_0_digital_signature.py) — a separate
        # user with reports:edit generates the PDF first.
        editor = create_user(session, tenant, email="editor@t1.example")
        reviewer = create_user(session, tenant, email="rev@t1.example", roles=(ROLE_REVIEWER,))
        report, _ = _create_report(session, tenant, branch, order)

        stub_pdf_render.succeed(make_pdf_bytes(1))
        client.post(f"/api/v1/reports/{report.id}/versions/1/generate-pdf", headers=auth_headers(editor))
        sign = client.post(f"/api/v1/reports/{report.id}/sign", json={}, headers=auth_headers(reviewer))
        assert sign.status_code == 200

        resp = client.get(f"/api/v1/reports/{report.id}/versions/1/pdf", headers=auth_headers(reviewer))
        assert resp.status_code == 200
        # H-0c Blocker B (§4): `POST /sign` now regenerates the official PDF
        # from the just-signed state (2 renders: the pre-signature one this
        # test creates, then the signing one). It previously published the
        # pre-signature PDF as-is, which is how a report could be marked
        # digitally signed while its authoritative PDF carried no signature.
        # The DOWNLOAD route still never regenerates — that half is unchanged
        # and is what this test is about.
        assert stub_pdf_render.call_count == 2
