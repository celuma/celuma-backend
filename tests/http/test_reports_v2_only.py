"""Céluma 1.4 (#22) — Reports V2 is the only authoring path.

Covers the retired tenant flag (stored true/false, PATCH compatibility), the
only report-creation route, the retired legacy letterhead export, and the
blocked state of unsigned legacy reports — while historical reports and their
artifacts stay readable and byte-for-byte unchanged.
"""
import uuid

import pytest
from sqlmodel import select

from app.models.enums import ReportStatus
from app.models.report import Report, ReportVersion
from app.models.tenant import Tenant

from .conftest import FakeS3Service, make_pdf_bytes
from .factories import (
    TEST_USER_PASSWORD,
    assign_reviewer,
    auth_headers,
    create_branch,
    create_order,
    create_report,
    create_tenant,
    create_user,
)
from .test_block_c_v2_template_version_independence import _author, _create_v2, _lab

LEGACY_CODE = "legacy_report_authoring_retired"


def _error_code(resp):
    detail = resp.json()["detail"]
    return detail.get("code") if isinstance(detail, dict) else None


def _report_count(session) -> int:
    return len(session.exec(select(Report)).all())


# ---------------------------------------------------------------------------
# Creation: one path, whatever the stored flag says
# ---------------------------------------------------------------------------

class TestCreationIsAlwaysV2:
    @pytest.mark.parametrize("stored_flag", [True, False])
    @pytest.mark.parametrize("selector", ["template_id", "template_version_id"])
    def test_stored_flag_is_not_read(self, client, session, stored_flag, selector):
        lab = _lab(session, v2=stored_flag)
        author = _author(session, lab)
        extra = {}
        if selector == "template_version_id":
            from app.models.report_template_version import (
                ReportTemplateVersion,
                ReportTemplateVersionStatus,
            )
            from .factories import valid_rendering_snapshot

            version = ReportTemplateVersion(
                tenant_id=lab["tenant"].id,
                report_template_id=lab["template"].id,
                version_number=1,
                schema_version=2,
                configuration=valid_rendering_snapshot(),
                status=ReportTemplateVersionStatus.ACTIVE,
            )
            session.add(version)
            session.commit()
            extra["template_version_id"] = str(version.id)

        resp, _ = _create_v2(client, lab, author, session, selector=selector, **extra)

        assert resp.status_code == 200, resp.text
        detail = client.get(
            f"/api/v1/reports/{resp.json()['id']}", headers=auth_headers(author)
        ).json()
        assert detail["schema_version"] == 2
        assert detail["report"]["rendering_snapshot"]["schema_version"] == 2

    @pytest.mark.parametrize("stored_flag", [True, False])
    def test_a_legacy_shaped_request_is_refused_and_creates_nothing(
        self, client, session, stored_flag
    ):
        lab = _lab(session, v2=stored_flag)
        author = _author(session, lab)
        order = create_order(session, lab["tenant"], lab["branch"], order_code="ORD-LEGACY")

        resp = client.post(
            "/api/v1/reports/",
            json={
                "tenant_id": str(lab["tenant"].id),
                "branch_id": str(lab["branch"].id),
                "order_id": str(order.id),
                "template": lab["template"].template_json,
                "report": {"base": {}, "sections": {}},
            },
            headers=auth_headers(author),
        )

        assert resp.status_code == 409, resp.text
        assert _error_code(resp) == "reports_v2_template_required"
        assert _report_count(session) == 0

    def test_a_foreign_tenant_template_is_not_found(self, client, session):
        lab = _lab(session)
        other = _lab(session, name="Otro laboratorio")
        author = _author(session, lab)
        # Own order/branch, someone else's template.
        lab_with_foreign_template = dict(lab, template=other["template"])

        resp, _ = _create_v2(client, lab_with_foreign_template, author, session)

        assert resp.status_code == 404, resp.text
        assert _report_count(session) == 0

    def test_missing_default_letterhead_is_an_actionable_block_not_a_fallback(
        self, client, session
    ):
        lab = _lab(session)
        lab["letterhead"].is_default = False
        session.add(lab["letterhead"])
        session.commit()
        author = _author(session, lab)

        resp, _ = _create_v2(client, lab, author, session)

        assert resp.status_code == 409, resp.text
        assert "membrete" in resp.json()["detail"].lower()
        assert _report_count(session) == 0

    def test_report_content_is_required(self, client, session):
        lab = _lab(session)
        author = _author(session, lab)

        resp, _ = _create_v2(client, lab, author, session, report=None)

        assert resp.status_code == 400, resp.text
        assert _report_count(session) == 0


# ---------------------------------------------------------------------------
# The retired tenant flag
# ---------------------------------------------------------------------------

class TestRetiredTenantFlag:
    def _admin(self, session, tenant):
        return create_user(session, tenant, email="admin@flag.example")

    @pytest.mark.parametrize("stored_flag", [True, False])
    def test_reads_always_report_true(self, client, session, stored_flag):
        tenant = create_tenant(session, reports_v2_enabled=stored_flag)
        admin = self._admin(session, tenant)

        detail = client.get(f"/api/v1/tenants/{tenant.id}", headers=auth_headers(admin))
        listing = client.get("/api/v1/tenants/", headers=auth_headers(admin))

        assert detail.json()["reports_v2_enabled"] is True
        assert listing.json()[0]["reports_v2_enabled"] is True

    def test_true_is_accepted_and_nothing_is_persisted(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=False)
        admin = self._admin(session, tenant)

        resp = client.patch(
            f"/api/v1/tenants/{tenant.id}",
            json={"reports_v2_enabled": True, "name": "Renombrado"},
            headers=auth_headers(admin),
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["reports_v2_enabled"] is True
        session.expire_all()
        stored = session.get(Tenant, tenant.id)
        assert stored.name == "Renombrado"
        assert stored.reports_v2_enabled is False  # compatibility column untouched

    def test_false_is_refused_before_anything_else_is_applied(self, client, session):
        tenant = create_tenant(session, name="Original", reports_v2_enabled=True)
        admin = self._admin(session, tenant)

        resp = client.patch(
            f"/api/v1/tenants/{tenant.id}",
            json={"reports_v2_enabled": False, "name": "No debe aplicarse"},
            headers=auth_headers(admin),
        )

        assert resp.status_code == 409, resp.text
        assert _error_code(resp) == "reports_v2_flag_retired"
        session.expire_all()
        stored = session.get(Tenant, tenant.id)
        assert stored.name == "Original"
        assert stored.reports_v2_enabled is True

    def test_omitting_the_field_is_unchanged_behaviour(self, client, session):
        tenant = create_tenant(session, reports_v2_enabled=False)
        admin = self._admin(session, tenant)

        resp = client.patch(
            f"/api/v1/tenants/{tenant.id}",
            json={"name": "Solo nombre"},
            headers=auth_headers(admin),
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "Solo nombre"


# ---------------------------------------------------------------------------
# Legacy letterhead export
# ---------------------------------------------------------------------------

class TestLegacyLetterheadExportRetired:
    URL = "/api/v1/report-letterheads/legacy/export"

    def test_authorized_request_gets_410(self, client, session):
        tenant = create_tenant(session)
        user = create_user(session, tenant, email="tpl@export.example")

        resp = client.get(self.URL, headers=auth_headers(user))

        assert resp.status_code == 410, resp.text
        assert _error_code(resp) == "legacy_letterhead_export_retired"

    def test_permission_check_is_not_bypassed(self, client, session):
        tenant = create_tenant(session)
        viewer = create_user(
            session, tenant, email="viewer@export.example", roles=("viewer",)
        )

        resp = client.get(self.URL, headers=auth_headers(viewer))

        assert resp.status_code == 403, resp.text

    def test_unauthenticated_request_is_refused(self, client):
        assert client.get(self.URL).status_code in (401, 403)


# ---------------------------------------------------------------------------
# Unsigned legacy reports: blocked, preserved, still readable
# ---------------------------------------------------------------------------

def _legacy(session, status, *, with_body=True, name="Lab legacy"):
    tenant = create_tenant(session, name=name, reports_v2_enabled=False)
    branch = create_branch(session, tenant)
    order = create_order(session, tenant, branch, order_code=f"ORD-{uuid.uuid4().hex[:6]}")
    report, version = create_report(
        session, tenant, branch, order, status=status, schema_version=None
    )
    if with_body:
        from .factories import create_storage_object

        key = f"reports/{tenant.id}/{branch.id}/{report.id}/versions/1/report.json"
        FakeS3Service.store[key] = b'{"base": {}, "sections": {}}'
        storage = create_storage_object(session, key=key, tenant=tenant)
        version.json_storage_id = storage.id
        session.add(version)
        session.commit()
        session.refresh(version)
    return tenant, branch, order, report, version


def _new_version_payload(tenant, branch, order):
    return {
        "tenant_id": str(tenant.id),
        "branch_id": str(branch.id),
        "order_id": str(order.id),
        "report": {"base": {}, "sections": {}},
    }


class TestUnsignedLegacyReportsAreBlocked:
    @pytest.mark.parametrize(
        "status", [ReportStatus.DRAFT, ReportStatus.IN_REVIEW]
    )
    def test_content_save_is_refused_and_nothing_changes(self, client, session, status):
        tenant, branch, order, report, version = _legacy(session, status)
        author = create_user(session, tenant, email="author@legacy.example")
        before_store = dict(FakeS3Service.store)

        resp = client.post(
            f"/api/v1/reports/{report.id}/new_version",
            json=_new_version_payload(tenant, branch, order),
            headers=auth_headers(author),
        )

        assert resp.status_code == 409, resp.text
        assert _error_code(resp) == LEGACY_CODE
        session.expire_all()
        versions = session.exec(
            select(ReportVersion).where(ReportVersion.report_id == report.id)
        ).all()
        assert [(v.version_no, v.is_current, v.schema_version) for v in versions] == [
            (1, True, None)
        ]
        assert FakeS3Service.store == before_store

    def test_submit_for_review_is_refused(self, client, session):
        tenant, branch, order, report, _ = _legacy(session, ReportStatus.DRAFT)
        author = create_user(session, tenant, email="author@legacy.example")

        resp = client.post(
            f"/api/v1/reports/{report.id}/submit", json={}, headers=auth_headers(author)
        )

        assert resp.status_code == 409, resp.text
        assert _error_code(resp) == LEGACY_CODE
        session.expire_all()
        assert session.get(Report, report.id).status == ReportStatus.DRAFT

    def test_official_pdf_generation_is_refused(self, client, session, stub_pdf_render):
        tenant, branch, order, report, version = _legacy(session, ReportStatus.APPROVED)
        author = create_user(session, tenant, email="author@legacy.example")
        stub_pdf_render.succeed(make_pdf_bytes(1))

        resp = client.post(
            f"/api/v1/reports/{report.id}/versions/1/generate-pdf",
            headers=auth_headers(author),
        )

        assert resp.status_code == 409, resp.text
        assert _error_code(resp) == LEGACY_CODE
        assert stub_pdf_render.call_count == 0
        session.expire_all()
        assert session.get(ReportVersion, version.id).pdf_storage_id is None

    @pytest.mark.parametrize("route", ["sign", "sign-and-publish"])
    def test_signing_is_refused(self, client, session, stub_pdf_render, route):
        tenant, branch, order, report, version = _legacy(session, ReportStatus.APPROVED)
        reviewer = create_user(
            session, tenant, email="reviewer@legacy.example", roles=("reviewer",)
        )
        assign_reviewer(session, order, reviewer)
        stub_pdf_render.succeed(make_pdf_bytes(1))

        resp = client.post(
            f"/api/v1/reports/{report.id}/{route}",
            json={"password": TEST_USER_PASSWORD},
            headers=auth_headers(reviewer),
        )

        assert resp.status_code == 409, resp.text
        assert _error_code(resp) == LEGACY_CODE
        assert stub_pdf_render.call_count == 0
        session.expire_all()
        assert session.get(Report, report.id).status == ReportStatus.APPROVED
        assert session.get(ReportVersion, version.id).signed_at is None

    def test_a_version_without_content_is_not_treated_as_v2(self, client, session):
        tenant, branch, order, report, version = _legacy(
            session, ReportStatus.DRAFT, with_body=False
        )
        author = create_user(session, tenant, email="author@legacy.example")

        resp = client.post(
            f"/api/v1/reports/{report.id}/new_version",
            json=_new_version_payload(tenant, branch, order),
            headers=auth_headers(author),
        )

        assert resp.status_code == 409, resp.text
        assert _error_code(resp) == LEGACY_CODE


class TestHistoricalLegacyReportsRemainAvailable:
    def test_an_unsigned_legacy_report_is_still_readable(self, client, session):
        tenant, branch, order, report, version = _legacy(session, ReportStatus.DRAFT)
        reader = create_user(session, tenant, email="reader@legacy.example")

        detail = client.get(f"/api/v1/reports/{report.id}", headers=auth_headers(reader))
        full = client.get(f"/api/v1/reports/{report.id}/full", headers=auth_headers(reader))

        assert detail.status_code == 200, detail.text
        assert detail.json()["schema_version"] is None
        assert full.status_code == 200, full.text

    def test_a_published_legacy_report_and_its_pdf_are_untouched(self, client, session):
        from .factories import create_storage_object

        tenant, branch, order, report, version = _legacy(session, ReportStatus.PUBLISHED)
        pdf_key = f"reports/{tenant.id}/{report.id}/official.pdf"
        FakeS3Service.store[pdf_key] = make_pdf_bytes(1)
        pdf_storage = create_storage_object(session, key=pdf_key, tenant=tenant)
        version.pdf_storage_id = pdf_storage.id
        version.pdf_generation_status = "READY"
        version.pdf_sha256 = "0" * 64
        version.pdf_size_bytes = len(FakeS3Service.store[pdf_key])
        version.pdf_page_count = 1
        session.add(version)
        session.commit()
        reader = create_user(session, tenant, email="reader@legacy.example")
        before_store = dict(FakeS3Service.store)

        detail = client.get(f"/api/v1/reports/{report.id}", headers=auth_headers(reader))
        pdf = client.get(f"/api/v1/reports/{report.id}/pdf", headers=auth_headers(reader))
        blocked_edit = client.post(
            f"/api/v1/reports/{report.id}/new_version",
            json=_new_version_payload(tenant, branch, order),
            headers=auth_headers(reader),
        )

        assert detail.status_code == 200, detail.text
        assert pdf.status_code == 200, pdf.text
        assert pdf.json()["pdf_storage_id"] == str(pdf_storage.id)
        # Already refused by the lifecycle (409), not by the legacy guard.
        assert blocked_edit.status_code == 409
        assert _error_code(blocked_edit) != LEGACY_CODE
        assert FakeS3Service.store == before_store
        session.expire_all()
        stored = session.get(ReportVersion, version.id)
        assert stored.pdf_sha256 == "0" * 64
        assert stored.schema_version is None

    def test_reviewer_verdicts_on_a_legacy_report_are_not_blocked(self, client, session):
        tenant, branch, order, report, version = _legacy(session, ReportStatus.IN_REVIEW)
        reviewer = create_user(
            session, tenant, email="reviewer@legacy.example", roles=("reviewer",)
        )
        assign_reviewer(session, order, reviewer, report=report)

        resp = client.post(
            f"/api/v1/reports/{report.id}/request-changes",
            json={"comment": "Revisar"},
            headers=auth_headers(reviewer),
        )

        assert resp.status_code == 200, resp.text
        session.expire_all()
        assert session.get(Report, report.id).status == ReportStatus.DRAFT


class TestV2ReportsAreNotAffectedByTheGuard:
    def test_v2_draft_keeps_its_authoring_routes(self, client, session):
        lab = _lab(session, v2=False)
        author = _author(session, lab)
        created, order = _create_v2(client, lab, author, session)
        assert created.status_code == 200, created.text
        report_id = created.json()["id"]

        saved = client.post(
            f"/api/v1/reports/{report_id}/new_version",
            json=_new_version_payload(lab["tenant"], lab["branch"], order),
            headers=auth_headers(author),
        )
        submitted = client.post(
            f"/api/v1/reports/{report_id}/submit", json={}, headers=auth_headers(author)
        )

        assert saved.status_code == 200, saved.text
        assert saved.json()["schema_version"] == 2
        # Submission may still fail for unrelated reasons (no reviewer); it
        # must never fail with the legacy-retirement code.
        assert _error_code(submitted) != LEGACY_CODE


# ---------------------------------------------------------------------------
# Remaining write routes (review of #22): PDF uploads and presentation PATCH
# ---------------------------------------------------------------------------

def _row_counts(session):
    from app.models.audit import AuditLog
    from app.models.storage import StorageObject

    session.expire_all()
    return (
        len(session.exec(select(StorageObject)).all()),
        len(session.exec(select(AuditLog)).all()),
    )


class TestLegacyPdfUploadAndPresentationAreBlocked:
    @pytest.mark.parametrize(
        "status", [ReportStatus.DRAFT, ReportStatus.IN_REVIEW, ReportStatus.APPROVED]
    )
    @pytest.mark.parametrize("suffix", ["/pdf", "/versions/1/pdf"])
    def test_pdf_upload_is_refused_without_any_side_effect(
        self, client, session, status, suffix
    ):
        tenant, branch, order, report, version = _legacy(session, status)
        author = create_user(session, tenant, email="upload@legacy.example")
        store_before = dict(FakeS3Service.store)
        counts_before = _row_counts(session)

        resp = client.post(
            f"/api/v1/reports/{report.id}{suffix}",
            files={"file": ("x.pdf", make_pdf_bytes(1), "application/pdf")},
            headers=auth_headers(author),
        )

        assert resp.status_code == 409, resp.text
        assert _error_code(resp) == LEGACY_CODE
        assert FakeS3Service.store == store_before
        assert _row_counts(session) == counts_before
        session.expire_all()
        assert session.get(ReportVersion, version.id).pdf_storage_id is None

    @pytest.mark.parametrize("status", [ReportStatus.DRAFT, ReportStatus.IN_REVIEW])
    def test_presentation_patch_is_refused_without_rewriting_the_stored_json(
        self, client, session, status
    ):
        tenant, branch, order, report, version = _legacy(session, status)
        reviewer = create_user(
            session, tenant, email="presentation@legacy.example", roles=("reviewer",)
        )
        assign_reviewer(session, order, reviewer, report=report)
        store_before = dict(FakeS3Service.store)
        counts_before = _row_counts(session)

        resp = client.patch(
            f"/api/v1/reports/{report.id}/presentation",
            json={"show_signature_section": True},
            headers=auth_headers(reviewer),
        )

        assert resp.status_code == 409, resp.text
        assert _error_code(resp) == LEGACY_CODE
        assert FakeS3Service.store == store_before
        assert _row_counts(session) == counts_before

    def test_upload_keeps_tenant_and_capability_guards_first(self, client, session):
        tenant, branch, order, report, version = _legacy(session, ReportStatus.DRAFT)
        other = create_tenant(session, name="Otro")
        outsider = create_user(session, other, email="outsider@legacy.example")
        viewer = create_user(
            session, tenant, email="viewer@legacy.example", roles=("viewer",)
        )
        files = {"file": ("x.pdf", make_pdf_bytes(1), "application/pdf")}

        foreign = client.post(
            f"/api/v1/reports/{report.id}/pdf", files=files, headers=auth_headers(outsider)
        )
        unprivileged = client.post(
            f"/api/v1/reports/{report.id}/pdf", files=files, headers=auth_headers(viewer)
        )

        assert foreign.status_code == 403
        assert unprivileged.status_code == 403

    @pytest.mark.parametrize("suffix", ["/pdf", "/versions/1/pdf"])
    def test_v2_draft_pdf_upload_still_works(self, client, session, suffix):
        tenant = create_tenant(session, name="V2 upload")
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch, order_code="ORD-V2UP")
        report, _ = create_report(session, tenant, branch, order, status=ReportStatus.DRAFT)
        author = create_user(session, tenant, email="v2upload@example.test")

        resp = client.post(
            f"/api/v1/reports/{report.id}{suffix}",
            files={"file": ("x.pdf", make_pdf_bytes(1), "application/pdf")},
            headers=auth_headers(author),
        )

        assert resp.status_code == 200, resp.text
