"""Céluma 1.3.1 Block A — the clinical reviewer authorization boundary.

Supersedes `test_block0_approval_rbac_repro.py`, whose `test_repro_*` cases
asserted the DEFECT (a 200 where the contract wants a 403). Those assertions
are inverted here; the rest are carried over unchanged so the legitimate paths
stay covered.

The contract under test (see `app/services/report_authorization.py`):

    ROLE_REVIEWER
      AND the reviewer capability for the action
      AND assigned as reviewer for the report's order
      AND same tenant
      AND a lifecycle state that permits the action

Two properties matter more than any single case and are tested explicitly:

  * **The role check, not the migration, is what holds.** Several tests grant
    `reports:approve` back to a pathologist at runtime to simulate a future
    broad permission grant, and assert approval is STILL refused. If someone
    later "simplifies" the guard back to a permission test, those fail.

  * **Nothing mutates on a rejected attempt.** Every rejection case asserts
    status, content, assignment, signature settings and letterhead are all
    unchanged — a 403 that still moved something is worse than no guard.
"""
import json
import uuid

import pytest
from sqlmodel import Session, select

from app.models.enums import ReportStatus, ReviewStatus
from app.models.permission import Permission
from app.models.report import Report, ReportVersion
from app.models.report_review import ReportReview
from app.models.role import Role
from app.models.role_permission import RolePermission
from app.models.storage import StorageObject

from .factories import (
    auth_headers,
    create_branch,
    create_order,
    create_tenant,
    create_user,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _report(session: Session, tenant, branch, order, *, status=ReportStatus.IN_REVIEW):
    report = Report(
        tenant_id=tenant.id,
        branch_id=branch.id,
        order_id=order.id,
        status=status,
        title="Clinical findings",
    )
    session.add(report)
    session.flush()
    version = ReportVersion(report_id=report.id, version_no=1, is_current=True)
    session.add(version)
    session.commit()
    session.refresh(report)
    session.refresh(version)
    return report, version


def _assign(session: Session, report, user, *, status=ReviewStatus.PENDING):
    review = ReportReview(
        tenant_id=report.tenant_id,
        order_id=report.order_id,
        report_id=report.id,
        reviewer_user_id=user.id,
        status=status,
    )
    session.add(review)
    session.commit()
    session.refresh(review)
    return review


def _grant(session: Session, role_code: str, permission_code: str):
    """Grant a permission to a role at runtime.

    Used to prove the role/assignment locks hold independently of the
    permission matrix — i.e. that re-granting `reports:approve` to
    `pathologist` (undoing migration v1_3_1) does NOT reopen CEL-131-01.
    """
    role = session.exec(select(Role).where(Role.code == role_code)).first()
    perm = session.exec(
        select(Permission).where(Permission.code == permission_code)
    ).first()
    existing = session.exec(
        select(RolePermission).where(
            RolePermission.role_id == role.id,
            RolePermission.permission_id == perm.id,
        )
    ).first()
    if not existing:
        session.add(RolePermission(role_id=role.id, permission_id=perm.id))
        session.commit()


def _store_body(session: Session, version: ReportVersion, tenant, body: dict):
    """Attach a persisted JSON body to a version, through the same fake S3 the
    application uses in tests."""
    from tests.http.conftest import FakeS3Service

    s3 = FakeS3Service()
    key = f"reports/{tenant.id}/versions/{version.id}/report.json"
    info = s3.upload_bytes(
        json.dumps(body, ensure_ascii=False).encode("utf-8"),
        key=key,
        content_type="application/json",
    )
    storage = StorageObject(
        provider="aws",
        region=s3.region,
        bucket=info.bucket,
        object_key=info.key,
        etag=info.etag,
        content_type="application/json",
        size_bytes=info.size_bytes,
        tenant_id=tenant.id,
    )
    session.add(storage)
    session.flush()
    version.json_storage_id = storage.id
    session.add(version)
    session.commit()
    session.refresh(version)
    return storage


def _read_body(session: Session, version: ReportVersion) -> dict:
    from tests.http.conftest import FakeS3Service

    storage = session.get(StorageObject, version.json_storage_id)
    return json.loads(FakeS3Service().download_text(storage.object_key))


class _Unchanged:
    """Snapshot of everything a rejected attempt must leave alone."""

    def __init__(self, session: Session, report: Report, version: ReportVersion):
        self.session = session
        self.report_id = report.id
        self.version_id = version.id
        self.status = report.status
        self.title = report.title
        self.letterhead = version.letterhead_version_id
        self.signed_by = version.signed_by
        self.signed_at = version.signed_at
        self.published_at = report.published_at
        self.pdf_status = version.pdf_generation_status
        self.reviews = {
            (r.reviewer_user_id, r.status)
            for r in session.exec(
                select(ReportReview).where(ReportReview.order_id == report.order_id)
            ).all()
        }
        self.body = (
            _read_body(session, version)
            if version.json_storage_id is not None
            else None
        )

    def assert_intact(self):
        self.session.expire_all()
        report = self.session.get(Report, self.report_id)
        version = self.session.get(ReportVersion, self.version_id)
        assert report.status == self.status, "report status changed after a rejection"
        assert report.title == self.title
        assert version.letterhead_version_id == self.letterhead, "letterhead changed"
        assert version.signed_by == self.signed_by, "a rejected attempt signed the report"
        assert version.signed_at == self.signed_at
        assert report.published_at == self.published_at
        assert version.pdf_generation_status == self.pdf_status, (
            "a publication artifact was produced by a rejected attempt"
        )
        reviews = {
            (r.reviewer_user_id, r.status)
            for r in self.session.exec(
                select(ReportReview).where(ReportReview.order_id == report.order_id)
            ).all()
        }
        assert reviews == self.reviews, "reviewer assignment changed"
        if self.body is not None:
            assert _read_body(self.session, version) == self.body, (
                "report content or signature settings changed"
            )


@pytest.fixture
def lab(session: Session):
    """One tenant, one branch, one order, one IN_REVIEW report."""
    tenant = create_tenant(session)
    branch = create_branch(session, tenant)
    order = create_order(session, tenant, branch)
    report, version = _report(session, tenant, branch, order)
    return {
        "tenant": tenant,
        "branch": branch,
        "order": order,
        "report": report,
        "version": version,
    }


# ---------------------------------------------------------------------------
# CEL-131-01 — approval
# ---------------------------------------------------------------------------

class TestApprovalAuthorization:
    def test_assigned_reviewer_approves(self, client, session, lab):
        reviewer = create_user(
            session, lab["tenant"], email="rev@t1.example", roles=("reviewer",)
        )
        review = _assign(session, lab["report"], reviewer)

        resp = client.post(
            f"/api/v1/reports/{lab['report'].id}/approve",
            json={},
            headers=auth_headers(reviewer),
        )
        assert resp.status_code == 200, resp.text

        session.expire_all()
        assert session.get(Report, lab["report"].id).status == ReportStatus.APPROVED
        assert session.get(ReportReview, review.id).status == ReviewStatus.APPROVED
        assert session.get(ReportReview, review.id).decision_at is not None

    def test_non_reviewer_pathologist_is_refused(self, client, session, lab):
        """CEL-131-01, the headline case. Block 0 reproduced this as a 200."""
        pathologist = create_user(
            session, lab["tenant"], email="path@t1.example", roles=("pathologist",)
        )
        before = _Unchanged(session, lab["report"], lab["version"])

        resp = client.post(
            f"/api/v1/reports/{lab['report'].id}/approve",
            json={"changelog": "looks fine to me"},
            headers=auth_headers(pathologist),
        )
        assert resp.status_code == 403, resp.text
        before.assert_intact()

    def test_a_pathologist_who_regains_the_permission_is_still_refused(
        self, client, session, lab
    ):
        """The load-bearing test for the whole block.

        Migration v1_3_1 removes `reports:approve` from `pathologist`, but a
        future migration, a manual grant, or a downgrade could put it back.
        The role and assignment locks must hold on their own — otherwise the
        vulnerability is one data change away from returning.
        """
        _grant(session, "pathologist", "reports:approve")
        pathologist = create_user(
            session, lab["tenant"], email="path2@t1.example", roles=("pathologist",)
        )
        before = _Unchanged(session, lab["report"], lab["version"])

        resp = client.post(
            f"/api/v1/reports/{lab['report'].id}/approve",
            json={},
            headers=auth_headers(pathologist),
        )
        assert resp.status_code == 403, resp.text
        before.assert_intact()

    def test_reviewer_not_assigned_to_this_report_is_refused(
        self, client, session, lab
    ):
        """Holds the role and the capability, but no assignment."""
        stranger = create_user(
            session, lab["tenant"], email="rev2@t1.example", roles=("reviewer",)
        )
        before = _Unchanged(session, lab["report"], lab["version"])

        resp = client.post(
            f"/api/v1/reports/{lab['report'].id}/approve",
            json={},
            headers=auth_headers(stranger),
        )
        assert resp.status_code == 403, resp.text
        before.assert_intact()

    def test_a_reviewer_assigned_to_a_different_order_is_refused(
        self, client, session, lab
    ):
        """Assignment is per order, so an assignment elsewhere must not leak."""
        other_order = create_order(
            session, lab["tenant"], lab["branch"], order_code="ORD-OTHER"
        )
        other_report, _ = _report(
            session, lab["tenant"], lab["branch"], other_order
        )
        reviewer = create_user(
            session, lab["tenant"], email="rev3@t1.example", roles=("reviewer",)
        )
        _assign(session, other_report, reviewer)
        before = _Unchanged(session, lab["report"], lab["version"])

        resp = client.post(
            f"/api/v1/reports/{lab['report'].id}/approve",
            json={},
            headers=auth_headers(reviewer),
        )
        assert resp.status_code == 403, resp.text
        before.assert_intact()

    def test_admin_is_refused(self, client, session, lab):
        admin = create_user(
            session, lab["tenant"], email="admin@t1.example", roles=("admin",)
        )
        before = _Unchanged(session, lab["report"], lab["version"])

        resp = client.post(
            f"/api/v1/reports/{lab['report'].id}/approve",
            json={},
            headers=auth_headers(admin),
        )
        assert resp.status_code == 403, resp.text
        before.assert_intact()

    def test_superuser_is_refused(self, client, session, lab):
        """Superuser holds `reports:approve` (it holds everything) but not the
        `reviewer` role. Block 0 reproduced this as a 200."""
        su = create_user(
            session, lab["tenant"], email="su@t1.example", roles=("superuser",)
        )
        before = _Unchanged(session, lab["report"], lab["version"])

        resp = client.post(
            f"/api/v1/reports/{lab['report'].id}/approve",
            json={},
            headers=auth_headers(su),
        )
        assert resp.status_code == 403, resp.text
        before.assert_intact()

    def test_reviewer_role_without_the_capability_is_refused(
        self, client, session, lab
    ):
        """The other half of the double lock, made representable by revoking
        `reports:approve` from the reviewer role at runtime."""
        role = session.exec(select(Role).where(Role.code == "reviewer")).first()
        perm = session.exec(
            select(Permission).where(Permission.code == "reports:approve")
        ).first()
        link = session.exec(
            select(RolePermission).where(
                RolePermission.role_id == role.id,
                RolePermission.permission_id == perm.id,
            )
        ).first()
        session.delete(link)
        session.commit()

        reviewer = create_user(
            session, lab["tenant"], email="rev4@t1.example", roles=("reviewer",)
        )
        _assign(session, lab["report"], reviewer)
        before = _Unchanged(session, lab["report"], lab["version"])

        resp = client.post(
            f"/api/v1/reports/{lab['report'].id}/approve",
            json={},
            headers=auth_headers(reviewer),
        )
        assert resp.status_code == 403, resp.text
        before.assert_intact()

    def test_cross_tenant_is_refused(self, client, session, lab):
        other_tenant = create_tenant(session, name="Other Lab")
        intruder = create_user(
            session, other_tenant, email="rev@t2.example", roles=("reviewer",)
        )
        before = _Unchanged(session, lab["report"], lab["version"])

        resp = client.post(
            f"/api/v1/reports/{lab['report'].id}/approve",
            json={},
            headers=auth_headers(intruder),
        )
        assert resp.status_code in (403, 404), resp.text
        before.assert_intact()

    def test_a_multi_role_user_is_judged_by_the_reviewer_contract(
        self, client, session, lab
    ):
        """Roles are additive. Holding `admin` as well as `reviewer` must not
        disqualify a legitimately assigned reviewer."""
        user = create_user(
            session,
            lab["tenant"],
            email="both@t1.example",
            roles=("admin", "reviewer"),
        )
        _assign(session, lab["report"], user)

        resp = client.post(
            f"/api/v1/reports/{lab['report'].id}/approve",
            json={},
            headers=auth_headers(user),
        )
        assert resp.status_code == 200, resp.text
        session.expire_all()
        assert session.get(Report, lab["report"].id).status == ReportStatus.APPROVED

    def test_a_draft_report_cannot_be_approved(self, client, session, lab):
        report = session.get(Report, lab["report"].id)
        report.status = ReportStatus.DRAFT
        session.add(report)
        session.commit()
        reviewer = create_user(
            session, lab["tenant"], email="rev5@t1.example", roles=("reviewer",)
        )
        _assign(session, report, reviewer)

        resp = client.post(
            f"/api/v1/reports/{report.id}/approve",
            json={},
            headers=auth_headers(reviewer),
        )
        assert resp.status_code == 400, resp.text


class TestRequestChangesAuthorization:
    """The rejection half of the same decision carried the identical defect."""

    def test_assigned_reviewer_can_request_changes(self, client, session, lab):
        reviewer = create_user(
            session, lab["tenant"], email="rev@t1.example", roles=("reviewer",)
        )
        review = _assign(session, lab["report"], reviewer)

        resp = client.post(
            f"/api/v1/reports/{lab['report'].id}/request-changes",
            json={"comment": "please revise the microscopic description"},
            headers=auth_headers(reviewer),
        )
        assert resp.status_code == 200, resp.text
        session.expire_all()
        assert session.get(Report, lab["report"].id).status == ReportStatus.DRAFT
        assert session.get(ReportReview, review.id).status == ReviewStatus.REJECTED

    def test_non_reviewer_pathologist_is_refused(self, client, session, lab):
        pathologist = create_user(
            session, lab["tenant"], email="path@t1.example", roles=("pathologist",)
        )
        before = _Unchanged(session, lab["report"], lab["version"])

        resp = client.post(
            f"/api/v1/reports/{lab['report'].id}/request-changes",
            json={"comment": "nope"},
            headers=auth_headers(pathologist),
        )
        assert resp.status_code == 403, resp.text
        before.assert_intact()

    def test_superuser_is_refused(self, client, session, lab):
        su = create_user(
            session, lab["tenant"], email="su@t1.example", roles=("superuser",)
        )
        before = _Unchanged(session, lab["report"], lab["version"])

        resp = client.post(
            f"/api/v1/reports/{lab['report'].id}/request-changes",
            json={"comment": "nope"},
            headers=auth_headers(su),
        )
        assert resp.status_code == 403, resp.text
        before.assert_intact()


# ---------------------------------------------------------------------------
# CEL-131-02 / A3 — signing
# ---------------------------------------------------------------------------

class TestSigningAuthorization:
    """Role + permission were already enforced; A3 adds assignment."""

    @pytest.fixture
    def approved(self, session, lab):
        report = session.get(Report, lab["report"].id)
        report.status = ReportStatus.APPROVED
        session.add(report)
        session.commit()
        session.refresh(report)
        return {**lab, "report": report}

    def test_assigned_reviewer_passes_authorization(
        self, client, session, approved, stub_pdf_render
    ):
        """The positive path reaches the publication machinery rather than a
        403. Asserted as "not 403" because the PDF/publication invariants are
        owned by test_report_sign_and_publish.py; this file is about the
        authorization boundary only."""
        from .conftest import make_pdf_bytes

        reviewer = create_user(
            session, approved["tenant"], email="rev@t1.example", roles=("reviewer",)
        )
        _assign(session, approved["report"], reviewer, status=ReviewStatus.APPROVED)
        stub_pdf_render.succeed(make_pdf_bytes(1))

        resp = client.post(
            f"/api/v1/reports/{approved['report'].id}/sign-and-publish",
            json={},
            headers=auth_headers(reviewer),
        )
        assert resp.status_code == 200, resp.text
        session.expire_all()
        assert (
            session.get(Report, approved["report"].id).status
            == ReportStatus.PUBLISHED
        )

    def test_unassigned_reviewer_is_refused(self, client, session, approved):
        """A3's new guard: reviewer B must not sign reviewer A's report."""
        reviewer_a = create_user(
            session, approved["tenant"], email="rev-a@t1.example", roles=("reviewer",)
        )
        _assign(session, approved["report"], reviewer_a, status=ReviewStatus.APPROVED)
        reviewer_b = create_user(
            session, approved["tenant"], email="rev-b@t1.example", roles=("reviewer",)
        )
        before = _Unchanged(session, approved["report"], approved["version"])

        resp = client.post(
            f"/api/v1/reports/{approved['report'].id}/sign-and-publish",
            json={},
            headers=auth_headers(reviewer_b),
        )
        assert resp.status_code == 403, resp.text
        before.assert_intact()

    def test_unassigned_reviewer_is_refused_on_the_legacy_sign_route(
        self, client, session, approved
    ):
        """`POST /{id}/sign` is not the UI path but remains a live,
        authenticated API — "the frontend doesn't call it" is not a safety
        argument (H-0c made the same point about this route)."""
        reviewer_b = create_user(
            session, approved["tenant"], email="rev-b@t1.example", roles=("reviewer",)
        )
        before = _Unchanged(session, approved["report"], approved["version"])

        resp = client.post(
            f"/api/v1/reports/{approved['report'].id}/sign",
            json={},
            headers=auth_headers(reviewer_b),
        )
        assert resp.status_code == 403, resp.text
        before.assert_intact()

    def test_pathologist_is_refused(self, client, session, approved):
        pathologist = create_user(
            session, approved["tenant"], email="path@t1.example", roles=("pathologist",)
        )
        before = _Unchanged(session, approved["report"], approved["version"])

        resp = client.post(
            f"/api/v1/reports/{approved['report'].id}/sign-and-publish",
            json={},
            headers=auth_headers(pathologist),
        )
        assert resp.status_code == 403, resp.text
        before.assert_intact()

    def test_admin_is_refused(self, client, session, approved):
        admin = create_user(
            session, approved["tenant"], email="admin@t1.example", roles=("admin",)
        )
        before = _Unchanged(session, approved["report"], approved["version"])

        resp = client.post(
            f"/api/v1/reports/{approved['report'].id}/sign-and-publish",
            json={},
            headers=auth_headers(admin),
        )
        assert resp.status_code == 403, resp.text
        before.assert_intact()

    def test_superuser_is_refused(self, client, session, approved):
        su = create_user(
            session, approved["tenant"], email="su@t1.example", roles=("superuser",)
        )
        before = _Unchanged(session, approved["report"], approved["version"])

        resp = client.post(
            f"/api/v1/reports/{approved['report'].id}/sign-and-publish",
            json={},
            headers=auth_headers(su),
        )
        assert resp.status_code == 403, resp.text
        assert "reviewer" in resp.text.lower()
        before.assert_intact()


# ---------------------------------------------------------------------------
# A4/A5 — reviewer presentation settings
# ---------------------------------------------------------------------------

class TestPresentationSettings:
    ENDPOINT = "/api/v1/reports/{}/presentation"

    @pytest.fixture
    def with_body(self, session, lab):
        _store_body(
            session,
            lab["version"],
            lab["tenant"],
            {
                "base": {"patient": "Doe, J."},
                "sections": [{"key": "micro", "text": "original clinical text"}],
                "signatureMetadata": {
                    "show_signature_section": False,
                    "require_digital_signature": False,
                },
            },
        )
        return lab

    def test_assigned_reviewer_can_change_signature_settings(
        self, client, session, with_body
    ):
        reviewer = create_user(
            session, with_body["tenant"], email="rev@t1.example", roles=("reviewer",)
        )
        _assign(session, with_body["report"], reviewer)

        resp = client.patch(
            self.ENDPOINT.format(with_body["report"].id),
            json={"show_signature_section": True, "require_digital_signature": True},
            headers=auth_headers(reviewer),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["show_signature_section"] is True
        assert resp.json()["require_digital_signature"] is True

        body = _read_body(session, with_body["version"])
        assert body["signatureMetadata"]["show_signature_section"] is True
        assert body["signatureMetadata"]["require_digital_signature"] is True

    def test_clinical_content_is_carried_through_untouched(
        self, client, session, with_body
    ):
        """The allowlist's whole point: a presentation change must not be a
        content edit."""
        reviewer = create_user(
            session, with_body["tenant"], email="rev@t1.example", roles=("reviewer",)
        )
        _assign(session, with_body["report"], reviewer)

        resp = client.patch(
            self.ENDPOINT.format(with_body["report"].id),
            json={"show_signature_section": True},
            headers=auth_headers(reviewer),
        )
        assert resp.status_code == 200, resp.text

        body = _read_body(session, with_body["version"])
        assert body["sections"] == [
            {"key": "micro", "text": "original clinical text"}
        ]
        assert body["base"] == {"patient": "Doe, J."}

    def test_arbitrary_clinical_fields_in_the_payload_are_ignored(
        self, client, session, with_body
    ):
        """A direct API call that smuggles clinical fields alongside the
        allowlisted ones must change only the allowlisted ones."""
        reviewer = create_user(
            session, with_body["tenant"], email="rev@t1.example", roles=("reviewer",)
        )
        _assign(session, with_body["report"], reviewer)

        resp = client.patch(
            self.ENDPOINT.format(with_body["report"].id),
            json={
                "show_signature_section": True,
                "sections": [{"key": "micro", "text": "INJECTED DIAGNOSIS"}],
                "base": {"patient": "SOMEONE ELSE"},
                "title": "rewritten",
                "status": "PUBLISHED",
            },
            headers=auth_headers(reviewer),
        )
        assert resp.status_code == 200, resp.text

        # The stored body must be untouched apart from signatureMetadata.
        body = _read_body(session, with_body["version"])
        assert body["sections"] == [
            {"key": "micro", "text": "original clinical text"}
        ]
        assert body["base"] == {"patient": "Doe, J."}
        session.expire_all()
        report = session.get(Report, with_body["report"].id)
        assert report.status == ReportStatus.IN_REVIEW
        assert report.title == "Clinical findings"

    def test_digital_signature_cannot_be_on_without_the_section(
        self, client, session, with_body
    ):
        """Server-side invariant, so a direct call cannot persist a state the
        renderer has nowhere to draw."""
        reviewer = create_user(
            session, with_body["tenant"], email="rev@t1.example", roles=("reviewer",)
        )
        _assign(session, with_body["report"], reviewer)

        resp = client.patch(
            self.ENDPOINT.format(with_body["report"].id),
            json={
                "show_signature_section": False,
                "require_digital_signature": True,
            },
            headers=auth_headers(reviewer),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["require_digital_signature"] is False
        body = _read_body(session, with_body["version"])
        assert body["signatureMetadata"]["require_digital_signature"] is False

    def test_non_reviewer_pathologist_is_refused(self, client, session, with_body):
        pathologist = create_user(
            session, with_body["tenant"], email="path@t1.example", roles=("pathologist",)
        )
        before = _Unchanged(session, with_body["report"], with_body["version"])

        resp = client.patch(
            self.ENDPOINT.format(with_body["report"].id),
            json={"show_signature_section": True},
            headers=auth_headers(pathologist),
        )
        assert resp.status_code == 403, resp.text
        before.assert_intact()

    def test_unassigned_reviewer_is_refused(self, client, session, with_body):
        stranger = create_user(
            session, with_body["tenant"], email="rev2@t1.example", roles=("reviewer",)
        )
        before = _Unchanged(session, with_body["report"], with_body["version"])

        resp = client.patch(
            self.ENDPOINT.format(with_body["report"].id),
            json={"show_signature_section": True},
            headers=auth_headers(stranger),
        )
        assert resp.status_code == 403, resp.text
        before.assert_intact()

    def test_admin_is_refused(self, client, session, with_body):
        admin = create_user(
            session, with_body["tenant"], email="admin@t1.example", roles=("admin",)
        )
        before = _Unchanged(session, with_body["report"], with_body["version"])

        resp = client.patch(
            self.ENDPOINT.format(with_body["report"].id),
            json={"show_signature_section": True},
            headers=auth_headers(admin),
        )
        assert resp.status_code == 403, resp.text
        before.assert_intact()

    def test_superuser_is_refused(self, client, session, with_body):
        su = create_user(
            session, with_body["tenant"], email="su@t1.example", roles=("superuser",)
        )
        before = _Unchanged(session, with_body["report"], with_body["version"])

        resp = client.patch(
            self.ENDPOINT.format(with_body["report"].id),
            json={"show_signature_section": True},
            headers=auth_headers(su),
        )
        assert resp.status_code == 403, resp.text
        before.assert_intact()

    def test_cross_tenant_is_refused_as_404(self, client, session, with_body):
        other = create_tenant(session, name="Other Lab")
        intruder = create_user(
            session, other, email="rev@t2.example", roles=("reviewer",)
        )
        before = _Unchanged(session, with_body["report"], with_body["version"])

        resp = client.patch(
            self.ENDPOINT.format(with_body["report"].id),
            json={"show_signature_section": True},
            headers=auth_headers(intruder),
        )
        assert resp.status_code == 404, resp.text
        before.assert_intact()

    def test_an_approved_report_is_frozen(self, client, session, with_body):
        """A5: after approval the presentation is frozen. The 1.3.1 route back
        is Block B's reopen, never a silent mutation."""
        report = session.get(Report, with_body["report"].id)
        report.status = ReportStatus.APPROVED
        session.add(report)
        session.commit()

        reviewer = create_user(
            session, with_body["tenant"], email="rev@t1.example", roles=("reviewer",)
        )
        _assign(session, report, reviewer, status=ReviewStatus.APPROVED)
        before = _Unchanged(session, report, with_body["version"])

        resp = client.patch(
            self.ENDPOINT.format(report.id),
            json={"show_signature_section": True},
            headers=auth_headers(reviewer),
        )
        assert resp.status_code == 409, resp.text
        before.assert_intact()

    def test_a_draft_report_is_not_the_reviewers_window(
        self, client, session, with_body
    ):
        """In DRAFT the author owns the document; the reviewer's narrow route
        does not apply."""
        report = session.get(Report, with_body["report"].id)
        report.status = ReportStatus.DRAFT
        session.add(report)
        session.commit()

        reviewer = create_user(
            session, with_body["tenant"], email="rev@t1.example", roles=("reviewer",)
        )
        _assign(session, report, reviewer)

        resp = client.patch(
            self.ENDPOINT.format(report.id),
            json={"show_signature_section": True},
            headers=auth_headers(reviewer),
        )
        assert resp.status_code == 409, resp.text

    def test_reviewer_still_cannot_use_the_content_path(
        self, client, session, with_body
    ):
        """The reviewer must NOT have gained `reports:edit`. If this starts
        passing with a 200, the narrow route has been widened into the
        generic "reviewer edits report" path A4 forbids."""
        reviewer = create_user(
            session, with_body["tenant"], email="rev@t1.example", roles=("reviewer",)
        )
        _assign(session, with_body["report"], reviewer)

        resp = client.post(
            f"/api/v1/reports/{with_body['report'].id}/new_version",
            json={
                "tenant_id": str(with_body["tenant"].id),
                "branch_id": str(with_body["branch"].id),
                "order_id": str(with_body["order"].id),
                "report": {"sections": [{"key": "micro", "text": "rewritten"}]},
            },
            headers=auth_headers(reviewer),
        )
        assert resp.status_code == 403, resp.text
        assert "reports:edit" in resp.text


# ---------------------------------------------------------------------------
# A7 — reviewer self-escalation
# ---------------------------------------------------------------------------

class TestReviewerSelfEscalation:
    """Block 0 finding F-5. `PUT /rbac/users/{id}/roles` needs only
    `admin:manage_users`, so an administrator could self-grant `reviewer` and
    acquire clinical authority — defeating the boundary CEL-131-01 builds."""

    def test_admin_cannot_grant_themselves_reviewer(self, client, session):
        tenant = create_tenant(session)
        admin = create_user(session, tenant, email="admin@t1.example", roles=("admin",))

        resp = client.put(
            f"/api/v1/rbac/users/{admin.id}/roles",
            json={"roles": ["admin", "reviewer"]},
            headers=auth_headers(admin),
        )
        assert resp.status_code == 403, resp.text
        session.expire_all()
        from app.core.rbac import get_user_roles

        assert "reviewer" not in get_user_roles(admin.id, session)

    def test_superuser_cannot_grant_themselves_reviewer(self, client, session):
        tenant = create_tenant(session)
        su = create_user(session, tenant, email="su@t1.example", roles=("superuser",))

        resp = client.put(
            f"/api/v1/rbac/users/{su.id}/roles",
            json={"roles": ["superuser", "reviewer"]},
            headers=auth_headers(su),
        )
        assert resp.status_code == 403, resp.text
        session.expire_all()
        from app.core.rbac import get_user_roles

        assert "reviewer" not in get_user_roles(su.id, session)

    def test_admin_cannot_grant_themselves_reviewer_via_the_user_endpoint(
        self, client, session
    ):
        """The second role-management route. `PUT /users/{id}` also replaces
        roles under `admin:manage_users`, so closing only the RBAC route would
        have left the boundary open here."""
        tenant = create_tenant(session)
        admin = create_user(session, tenant, email="admin@t1.example", roles=("admin",))

        resp = client.put(
            f"/api/v1/users/{admin.id}",
            json={"role": "reviewer"},
            headers=auth_headers(admin),
        )
        assert resp.status_code == 403, resp.text
        session.expire_all()
        from app.core.rbac import get_user_roles

        assert "reviewer" not in get_user_roles(admin.id, session)

    def test_admin_may_still_set_another_users_role_to_reviewer(self, client, session):
        """The same two-person rule on the second route."""
        tenant = create_tenant(session)
        admin = create_user(session, tenant, email="admin@t1.example", roles=("admin",))
        colleague = create_user(
            session, tenant, email="doc@t1.example", roles=("pathologist",)
        )

        resp = client.put(
            f"/api/v1/users/{colleague.id}",
            json={"role": "reviewer"},
            headers=auth_headers(admin),
        )
        assert resp.status_code == 200, resp.text
        session.expire_all()
        from app.core.rbac import get_user_roles

        assert "reviewer" in get_user_roles(colleague.id, session)

    def test_admin_may_still_grant_reviewer_to_someone_else(self, client, session):
        """The product contract has administrators manage who reviews — and
        `PUT /orders/{id}/reviewers` depends on it. Only SELF-assignment is
        blocked, which makes it a two-person rule rather than a prohibition."""
        tenant = create_tenant(session)
        admin = create_user(session, tenant, email="admin@t1.example", roles=("admin",))
        colleague = create_user(
            session, tenant, email="doc@t1.example", roles=("pathologist",)
        )

        resp = client.put(
            f"/api/v1/rbac/users/{colleague.id}/roles",
            json={"roles": ["pathologist", "reviewer"]},
            headers=auth_headers(admin),
        )
        assert resp.status_code == 200, resp.text
        assert "reviewer" in resp.json()["roles"]

    def test_an_existing_admin_reviewer_may_keep_both_roles(self, client, session):
        """Multi-role users stay legal. Re-submitting an unchanged role set
        must not be read as an escalation attempt."""
        tenant = create_tenant(session)
        other_admin = create_user(
            session, tenant, email="admin2@t1.example", roles=("admin",)
        )
        both = create_user(
            session, tenant, email="both@t1.example", roles=("admin", "reviewer")
        )

        resp = client.put(
            f"/api/v1/rbac/users/{both.id}/roles",
            json={"roles": ["admin", "reviewer"]},
            headers=auth_headers(both),
        )
        assert resp.status_code == 200, resp.text
        assert set(resp.json()["roles"]) == {"admin", "reviewer"}
        assert other_admin is not None

    def test_dropping_your_own_reviewer_role_is_allowed(self, client, session):
        """Shedding privilege is not escalation."""
        tenant = create_tenant(session)
        both = create_user(
            session, tenant, email="both@t1.example", roles=("admin", "reviewer")
        )

        resp = client.put(
            f"/api/v1/rbac/users/{both.id}/roles",
            json={"roles": ["admin"]},
            headers=auth_headers(both),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["roles"] == ["admin"]

    def test_self_escalation_does_not_yield_approval_authority(
        self, client, session, lab
    ):
        """End-to-end statement of what F-5 was worth: the admin cannot take
        the role, and therefore cannot approve."""
        admin = create_user(
            session, lab["tenant"], email="admin@t1.example", roles=("admin",)
        )
        client.put(
            f"/api/v1/rbac/users/{admin.id}/roles",
            json={"roles": ["admin", "reviewer"]},
            headers=auth_headers(admin),
        )
        before = _Unchanged(session, lab["report"], lab["version"])

        resp = client.post(
            f"/api/v1/reports/{lab['report'].id}/approve",
            json={},
            headers=auth_headers(admin),
        )
        assert resp.status_code == 403, resp.text
        before.assert_intact()


# ---------------------------------------------------------------------------
# A8 — the reopen authorization contract Block B will reuse
# ---------------------------------------------------------------------------

class TestReopenAuthorizationContract:
    """Block A does NOT implement the APPROVED → DRAFT transition. It fixes
    the policy predicate Block B must call, and tests it directly so the
    boundary is settled before the transition exists."""

    @pytest.fixture
    def approved(self, session, lab):
        report = session.get(Report, lab["report"].id)
        report.status = ReportStatus.APPROVED
        session.add(report)
        session.commit()
        session.refresh(report)
        return {**lab, "report": report}

    def test_assigned_reviewer_is_eligible(self, session, approved):
        from app.services.report_authorization import can_reopen_approved_report

        reviewer = create_user(
            session, approved["tenant"], email="rev@t1.example", roles=("reviewer",)
        )
        _assign(session, approved["report"], reviewer)
        assert can_reopen_approved_report(session, approved["report"], reviewer)

    def test_admin_is_eligible(self, session, approved):
        from app.services.report_authorization import can_reopen_approved_report

        admin = create_user(
            session, approved["tenant"], email="admin@t1.example", roles=("admin",)
        )
        assert can_reopen_approved_report(session, approved["report"], admin)

    def test_superuser_is_eligible(self, session, approved):
        from app.services.report_authorization import can_reopen_approved_report

        su = create_user(
            session, approved["tenant"], email="su@t1.example", roles=("superuser",)
        )
        assert can_reopen_approved_report(session, approved["report"], su)

    def test_non_reviewer_pathologist_is_not_eligible(self, session, approved):
        from app.services.report_authorization import can_reopen_approved_report

        pathologist = create_user(
            session, approved["tenant"], email="path@t1.example", roles=("pathologist",)
        )
        assert not can_reopen_approved_report(session, approved["report"], pathologist)

    def test_unassigned_reviewer_is_not_eligible(self, session, approved):
        from app.services.report_authorization import can_reopen_approved_report

        stranger = create_user(
            session, approved["tenant"], email="rev2@t1.example", roles=("reviewer",)
        )
        assert not can_reopen_approved_report(session, approved["report"], stranger)

    def test_reopen_eligibility_does_not_confer_approval_or_signing(
        self, client, session, approved
    ):
        """The separation A8 insists on: admin may reopen (Block B) but must
        never gain clinical authority from that capability."""
        from app.services.report_authorization import can_reopen_approved_report

        admin = create_user(
            session, approved["tenant"], email="admin@t1.example", roles=("admin",)
        )
        assert can_reopen_approved_report(session, approved["report"], admin)

        before = _Unchanged(session, approved["report"], approved["version"])
        sign = client.post(
            f"/api/v1/reports/{approved['report'].id}/sign-and-publish",
            json={},
            headers=auth_headers(admin),
        )
        assert sign.status_code == 403, sign.text
        presentation = client.patch(
            f"/api/v1/reports/{approved['report'].id}/presentation",
            json={"show_signature_section": True},
            headers=auth_headers(admin),
        )
        assert presentation.status_code == 403, presentation.text
        # 403 rather than 409: authorization is evaluated before lifecycle
        # state, so an unauthorized caller never learns the report's status.
        before.assert_intact()

    def test_the_transition_honours_this_predicate(self, client, session, approved):
        """Block B implements the transition (`POST /reports/{id}/reopen`) and
        must keep calling THIS predicate and nothing wider. Block A asserted
        the endpoint did not exist yet; now that it does, the boundary worth
        guarding is that the route's answer agrees with the policy — if
        someone widens the route's own check, the two diverge here.

        The transition's own guards (unsigned, lifecycle state, audit) are
        covered by `test_block_b_report_reopen.py`.
        """
        from app.services.report_authorization import can_reopen_approved_report

        pathologist = create_user(
            session, approved["tenant"], email="path@t1.example", roles=("pathologist",)
        )
        assert not can_reopen_approved_report(session, approved["report"], pathologist)
        refused = client.post(
            f"/api/v1/reports/{approved['report'].id}/reopen",
            json={},
            headers=auth_headers(pathologist),
        )
        assert refused.status_code == 403, refused.text
        session.expire_all()
        assert (
            session.get(Report, approved["report"].id).status == ReportStatus.APPROVED
        )

        admin = create_user(
            session, approved["tenant"], email="admin@t1.example", roles=("admin",)
        )
        assert can_reopen_approved_report(session, approved["report"], admin)
        allowed = client.post(
            f"/api/v1/reports/{approved['report'].id}/reopen",
            json={},
            headers=auth_headers(admin),
        )
        assert allowed.status_code == 200, allowed.text
        session.expire_all()
        assert session.get(Report, approved["report"].id).status == ReportStatus.DRAFT


# ---------------------------------------------------------------------------
# A5/A6 corrected — the author boundary holds in DRAFT too
# ---------------------------------------------------------------------------

class TestAuthorCannotTouchPresentationInDraft:
    """The corrected product contract: the signature settings and the
    letterhead are reviewer-only in EVERY state, not merely after submission.

    The first cut of Block A left them with the author in DRAFT on the
    reasoning that DRAFT belongs to the author. It does — for clinical
    CONTENT. These three fields decide what the final document asserts about
    who signed it and under whose letterhead, which is the reviewer's
    responsibility throughout.

    These tests go through the API directly rather than the editor, because
    hiding a control is a UX boundary and this is the security one.
    """

    @pytest.fixture
    def draft(self, session, lab):
        report = session.get(Report, lab["report"].id)
        report.status = ReportStatus.DRAFT
        session.add(report)
        session.commit()
        _store_body(
            session,
            lab["version"],
            lab["tenant"],
            {
                "sections": [{"key": "micro", "text": "original clinical text"}],
                "signatureMetadata": {
                    "show_signature_section": False,
                    "require_digital_signature": False,
                },
            },
        )
        session.refresh(report)
        return {**lab, "report": report}

    def _save(self, client, session, draft, author, body):
        return client.post(
            f"/api/v1/reports/{draft['report'].id}/new_version",
            json={
                "tenant_id": str(draft["tenant"].id),
                "branch_id": str(draft["branch"].id),
                "order_id": str(draft["order"].id),
                "report": body,
            },
            headers=auth_headers(author),
        )

    def test_a_draft_save_cannot_turn_on_the_signature_section(
        self, client, session, draft
    ):
        author = create_user(
            session, draft["tenant"], email="path@t1.example", roles=("pathologist",)
        )

        resp = self._save(
            client,
            session,
            draft,
            author,
            {
                "sections": [{"key": "micro", "text": "edited clinical text"}],
                "signatureMetadata": {
                    "show_signature_section": True,
                    "require_digital_signature": True,
                },
            },
        )
        assert resp.status_code == 200, resp.text

        # The clinical edit landed; the presentation fields did not move.
        session.expire_all()
        current = session.exec(
            select(ReportVersion).where(
                ReportVersion.report_id == draft["report"].id,
                ReportVersion.is_current == True,  # noqa: E712
            )
        ).first()
        body = _read_body(session, current)
        assert body["sections"] == [{"key": "micro", "text": "edited clinical text"}]
        assert body["signatureMetadata"]["show_signature_section"] is False
        assert body["signatureMetadata"]["require_digital_signature"] is False

    def test_a_draft_save_cannot_forge_a_signature_url(self, client, session, draft):
        """`signature_url` is written at signing time by the backend. A draft
        save must be unable to invent one — that would put a signature image
        on a report nobody signed."""
        author = create_user(
            session, draft["tenant"], email="path@t1.example", roles=("pathologist",)
        )

        resp = self._save(
            client,
            session,
            draft,
            author,
            {
                "sections": [{"key": "micro", "text": "x"}],
                "signatureMetadata": {
                    "show_signature_section": True,
                    "require_digital_signature": True,
                    "signature_url": "https://evil.example/forged-signature.png",
                },
            },
        )
        assert resp.status_code == 200, resp.text

        session.expire_all()
        current = session.exec(
            select(ReportVersion).where(
                ReportVersion.report_id == draft["report"].id,
                ReportVersion.is_current == True,  # noqa: E712
            )
        ).first()
        assert "signature_url" not in _read_body(session, current)["signatureMetadata"]

    def test_a_draft_save_cannot_change_the_letterhead(self, client, session, draft):
        """Rejected, not ignored: a letterhead is chosen explicitly, so
        silently keeping the old one would leave the caller believing the
        change landed."""
        author = create_user(
            session, draft["tenant"], email="path@t1.example", roles=("pathologist",)
        )

        resp = client.post(
            f"/api/v1/reports/{draft['report'].id}/new_version",
            json={
                "tenant_id": str(draft["tenant"].id),
                "branch_id": str(draft["branch"].id),
                "order_id": str(draft["order"].id),
                "report": {"sections": []},
                "letterhead_version_id": str(uuid.uuid4()),
            },
            headers=auth_headers(author),
        )
        assert resp.status_code == 403, resp.text
        assert "revisor" in resp.text.lower()

    def test_an_ordinary_content_save_still_works(self, client, session, draft):
        """The boundary must not break authoring: a save that does not touch
        presentation behaves exactly as before."""
        author = create_user(
            session, draft["tenant"], email="path@t1.example", roles=("pathologist",)
        )

        resp = self._save(
            client,
            session,
            draft,
            author,
            {"sections": [{"key": "micro", "text": "a normal edit"}]},
        )
        assert resp.status_code == 200, resp.text

        session.expire_all()
        current = session.exec(
            select(ReportVersion).where(
                ReportVersion.report_id == draft["report"].id,
                ReportVersion.is_current == True,  # noqa: E712
            )
        ).first()
        body = _read_body(session, current)
        assert body["sections"] == [{"key": "micro", "text": "a normal edit"}]
        assert body["signatureMetadata"] == {
            "show_signature_section": False,
            "require_digital_signature": False,
        }

    def test_the_reviewer_route_remains_the_only_way_in(self, client, session, lab):
        """The counterpart: what the author cannot do, the assigned reviewer
        still can, through the narrow route, once the report is IN_REVIEW."""
        _store_body(
            session,
            lab["version"],
            lab["tenant"],
            {"sections": [], "signatureMetadata": {"show_signature_section": False}},
        )
        reviewer = create_user(
            session, lab["tenant"], email="rev@t1.example", roles=("reviewer",)
        )
        _assign(session, lab["report"], reviewer)

        resp = client.patch(
            f"/api/v1/reports/{lab['report'].id}/presentation",
            json={"show_signature_section": True},
            headers=auth_headers(reviewer),
        )
        assert resp.status_code == 200, resp.text
        assert _read_body(session, lab["version"])["signatureMetadata"][
            "show_signature_section"
        ] is True
