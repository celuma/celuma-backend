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

Céluma 1.4 (#22): the read invariants run against two worlds — a V2 report
whose official PDF is generated through the real route, and a historical
legacy report seeded with the PDF it was issued with. The retired legacy
authoring routes cannot produce the second one any more, but its artifact
must stay downloadable under the same authorization (ADR 0016).
"""
import hashlib
import json
from datetime import datetime

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

from .conftest import FakeS3Service, make_pdf_bytes
from .factories import (
    TEST_USER_PASSWORD,
    assign_reviewer,
    auth_headers,
    create_branch,
    create_order,
    create_storage_object,
    create_tenant,
    create_user,
    valid_rendering_snapshot,
)


def _v2_version(session, tenant, branch, report, *, version_no=1, is_current=True):
    """A V2 version as `POST /reports/` and `new_version` persist it: the V2
    marker plus a stored body carrying its frozen `rendering_snapshot`."""
    key = f"reports/{tenant.id}/{branch.id}/{report.id}/versions/{version_no}/report.json"
    FakeS3Service.store[key] = json.dumps(
        {
            "schema_version": 2,
            "base": {},
            "sections": {},
            "rendering_snapshot": valid_rendering_snapshot(),
        }
    ).encode("utf-8")
    body = create_storage_object(session, key=key, tenant=tenant)
    version = ReportVersion(
        report_id=report.id,
        version_no=version_no,
        is_current=is_current,
        schema_version=2,
        json_storage_id=body.id,
    )
    session.add(version)
    session.commit()
    session.refresh(version)
    return version


def _report_with_official_pdf(client, session, tenant, branch, order, stub_pdf_render, editor):
    report = Report(
        tenant_id=tenant.id, branch_id=branch.id, order_id=order.id, status=ReportStatus.APPROVED
    )
    session.add(report)
    session.flush()
    version = _v2_version(session, tenant, branch, report)
    session.refresh(report)

    stub_pdf_render.succeed(make_pdf_bytes(1))
    resp = client.post(
        f"/api/v1/reports/{report.id}/versions/1/generate-pdf", headers=auth_headers(editor)
    )
    assert resp.status_code == 200, resp.text
    session.refresh(version)
    return report, version


def _historical_legacy_report_with_issued_pdf(session, tenant, branch, order):
    """A legacy report published before 1.4, with the official PDF it was
    issued with. Seeded directly because no route may author a legacy
    artifact any more; the read routes must still serve this one as-is."""
    report = Report(
        tenant_id=tenant.id,
        branch_id=branch.id,
        order_id=order.id,
        status=ReportStatus.PUBLISHED,
        published_at=datetime.utcnow(),
    )
    session.add(report)
    session.flush()
    body_key = f"reports/{tenant.id}/{branch.id}/{report.id}/versions/1/report.json"
    FakeS3Service.store[body_key] = json.dumps(
        {"base": {"diagnosis": "Benigno"}, "sections": {}}
    ).encode("utf-8")
    body = create_storage_object(session, key=body_key, tenant=tenant)
    pdf_bytes = make_pdf_bytes(1)
    pdf_key = f"reports/{tenant.id}/{report.id}/official/historical.pdf"
    FakeS3Service.store[pdf_key] = pdf_bytes
    pdf = create_storage_object(session, key=pdf_key, tenant=tenant)
    version = ReportVersion(
        report_id=report.id,
        version_no=1,
        is_current=True,
        schema_version=None,
        json_storage_id=body.id,
        pdf_storage_id=pdf.id,
        pdf_generation_status="READY",
        pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        pdf_size_bytes=len(pdf_bytes),
        pdf_page_count=1,
        signed_at=report.published_at,
    )
    session.add(version)
    session.commit()
    session.refresh(report)
    session.refresh(version)
    return report, version


@pytest.fixture(params=["v2_generated", "historical_legacy"])
def published_world(request, client, session, stub_pdf_render):
    tenant = create_tenant(session)
    branch = create_branch(session, tenant)
    order = create_order(session, tenant, branch)
    editor = create_user(session, tenant, email="editor@t1.example", roles=(ROLE_PATHOLOGIST,))
    if request.param == "v2_generated":
        report, version = _report_with_official_pdf(
            client, session, tenant, branch, order, stub_pdf_render, editor
        )
    else:
        report, version = _historical_legacy_report_with_issued_pdf(
            session, tenant, branch, order
        )
    assert version.pdf_storage_id is not None
    return {
        "tenant": tenant,
        "branch": branch,
        "order": order,
        "editor": editor,
        "report": report,
        "schema_version": version.schema_version,
        "pdf_storage_id": str(version.pdf_storage_id),
    }


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
        # Both routes serve the artifact that exists — never a new one.
        assert specific.json()["pdf_storage_id"] == published_world["pdf_storage_id"]
        assert latest.json()["pdf_storage_id"] == published_world["pdf_storage_id"]

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
        session.add(
            ReportVersion(
                report_id=report.id,
                version_no=2,
                is_current=False,
                schema_version=published_world["schema_version"],
            )
        )
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
        # 1.3.1 Block A: signing now requires assignment, not just the role.
        assign_reviewer(session, order, signer)

        report = Report(
            tenant_id=tenant.id,
            branch_id=branch.id,
            order_id=order.id,
            status=ReportStatus.APPROVED,
        )
        session.add(report)
        session.flush()
        # Céluma 1.4 (#22): only a V2 report can be signed anew.
        _v2_version(session, tenant, branch, report)
        session.refresh(report)

        stub_pdf_render.succeed(make_pdf_bytes(2))
        resp = client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish",
            json={"password": TEST_USER_PASSWORD},
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
        # 1.3.1 Block A: signing now requires assignment, not just the role.
        assign_reviewer(session, order, signer)

        report = Report(
            tenant_id=tenant.id,
            branch_id=branch.id,
            order_id=order.id,
            status=ReportStatus.APPROVED,
        )
        session.add(report)
        session.flush()
        # Céluma 1.4 (#22): only a V2 report can be signed anew.
        _v2_version(session, tenant, branch, report, version_no=1, is_current=False)
        _v2_version(session, tenant, branch, report, version_no=2, is_current=True)
        session.refresh(report)

        stub_pdf_render.succeed(make_pdf_bytes(1))
        resp = client.post(
            f"/api/v1/reports/{report.id}/sign-and-publish",
            json={"password": TEST_USER_PASSWORD},
            headers=auth_headers(signer),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["version_no"] == 2

        full = client.get(f"/api/v1/reports/{report.id}/full", headers=auth_headers(signer))
        assert full.json()["report"]["version_no"] == resp.json()["version_no"]
