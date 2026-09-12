"""Céluma 1.3.1 Block B — reopening an approved but unsigned report.

CEL-131-03. The transition under test:

    APPROVED + UNSIGNED  ->  DRAFT

and then the ordinary lifecycle again: DRAFT -> submit -> IN_REVIEW ->
reviewer approves -> APPROVED -> sign. A reopened report skips nothing.

Three properties matter more than any individual case, and each has its own
section below:

  * **A signed report is never reopenable.** The guard is signature EVIDENCE
    on the report's versions (`signed_at` / `signed_by`) plus
    `Report.published_at`, not `Report.status` — status and signature are
    separate facts written at different moments. Amending a signed report is
    the Céluma 1.4 workflow and is out of scope here entirely.

  * **Reopening confers nothing else.** An administrator may reopen and still
    cannot approve, sign, or touch presentation settings; a reviewer who
    reopens cannot sign until the report has been approved again. The reopen
    capability (`reports:manage_templates`) exists to let an administrator
    unstick a report, not to give them clinical authority — that bypass is
    precisely what CEL-131-01 removed.

  * **Nothing mutates on a rejected attempt**, and nothing historical is
    destroyed on a successful one. Every rejection asserts the full snapshot
    is intact; the success path asserts the previous review decision and the
    approval audit row survive.
"""
import uuid
from datetime import datetime

import pytest
from sqlmodel import Session, select

from app.models.audit import AuditLog
from app.models.enums import ReportStatus, ReviewStatus
from app.models.report import Report, ReportVersion
from app.models.report_review import ReportReview

from .factories import (
    assign_reviewer,
    auth_headers,
    create_branch,
    create_order,
    create_report,
    create_tenant,
    create_user,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reopen(client, report, actor, **kwargs):
    return client.post(
        f"/api/v1/reports/{report.id}/reopen",
        json=kwargs.get("body", {}),
        headers=auth_headers(actor),
    )


def _set_status(session: Session, report: Report, status: ReportStatus):
    report.status = status
    session.add(report)
    session.commit()
    session.refresh(report)
    return report


class _Unchanged:
    """Snapshot of everything a rejected reopen must leave alone.

    Wider than "the report status": a guard that returned 403 but had already
    cleared a signature, reset a reviewer or dropped the official PDF would be
    worse than no guard at all.
    """

    def __init__(self, session: Session, report: Report):
        self.session = session
        self.report_id = report.id
        self.order_id = report.order_id
        self.status = report.status
        self.published_at = report.published_at
        self.versions = {
            v.id: (
                v.version_no,
                v.is_current,
                v.signed_by,
                v.signed_at,
                v.pdf_generation_status,
                v.pdf_storage_id,
                v.letterhead_version_id,
            )
            for v in session.exec(
                select(ReportVersion).where(ReportVersion.report_id == report.id)
            ).all()
        }
        self.reviews = {
            r.id: (r.reviewer_user_id, r.status, r.decision_at)
            for r in session.exec(
                select(ReportReview).where(ReportReview.order_id == report.order_id)
            ).all()
        }

    def assert_intact(self):
        self.session.expire_all()
        report = self.session.get(Report, self.report_id)
        assert report.status == self.status, "a rejected reopen changed the status"
        assert report.published_at == self.published_at, "publication state changed"
        versions = {
            v.id: (
                v.version_no,
                v.is_current,
                v.signed_by,
                v.signed_at,
                v.pdf_generation_status,
                v.pdf_storage_id,
                v.letterhead_version_id,
            )
            for v in self.session.exec(
                select(ReportVersion).where(ReportVersion.report_id == self.report_id)
            ).all()
        }
        assert versions == self.versions, (
            "a rejected reopen changed the version, its signature or its PDF"
        )
        reviews = {
            r.id: (r.reviewer_user_id, r.status, r.decision_at)
            for r in self.session.exec(
                select(ReportReview).where(ReportReview.order_id == self.order_id)
            ).all()
        }
        assert reviews == self.reviews, "reviewer assignment or decision changed"


def _audit(session: Session, report: Report, action: str):
    return session.exec(
        select(AuditLog).where(
            AuditLog.entity_id == report.id,
            AuditLog.action == action,
        )
    ).all()


@pytest.fixture
def lab(session: Session):
    """One tenant, one branch, one order, one APPROVED + UNSIGNED report with
    an assigned reviewer whose decision is already recorded — the state a real
    report is in when someone asks to reopen it."""
    tenant = create_tenant(session)
    branch = create_branch(session, tenant)
    order = create_order(session, tenant, branch)
    report, version = create_report(
        session, tenant, branch, order, status=ReportStatus.APPROVED
    )
    reviewer = create_user(
        session, tenant, email="rev@t1.example", roles=("reviewer",)
    )
    review = assign_reviewer(
        session, order, reviewer, report=report, status=ReviewStatus.APPROVED
    )
    review.decision_at = datetime.utcnow()
    session.add(review)
    session.commit()
    session.refresh(review)
    return {
        "tenant": tenant,
        "branch": branch,
        "order": order,
        "report": report,
        "version": version,
        "reviewer": reviewer,
        "review": review,
    }


# ---------------------------------------------------------------------------
# Authorized transitions
# ---------------------------------------------------------------------------

class TestAuthorizedReopen:
    def test_assigned_reviewer_reopens(self, client, session, lab):
        resp = _reopen(client, lab["report"], lab["reviewer"])
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == ReportStatus.DRAFT

        session.expire_all()
        assert session.get(Report, lab["report"].id).status == ReportStatus.DRAFT

    def test_admin_reopens(self, client, session, lab):
        admin = create_user(
            session, lab["tenant"], email="admin@t1.example", roles=("admin",)
        )
        resp = _reopen(client, lab["report"], admin)
        assert resp.status_code == 200, resp.text

        session.expire_all()
        assert session.get(Report, lab["report"].id).status == ReportStatus.DRAFT

    def test_superuser_reopens(self, client, session, lab):
        su = create_user(
            session, lab["tenant"], email="su@t1.example", roles=("superuser",)
        )
        resp = _reopen(client, lab["report"], su)
        assert resp.status_code == 200, resp.text

        session.expire_all()
        assert session.get(Report, lab["report"].id).status == ReportStatus.DRAFT

    def test_a_reviewer_who_also_holds_admin_reopens(self, client, session, lab):
        """Roles are additive — holding another role is never a reason to
        reject someone who satisfies the contract."""
        both = create_user(
            session, lab["tenant"], email="both@t1.example", roles=("admin", "reviewer")
        )
        assign_reviewer(session, lab["order"], both, report=lab["report"])
        resp = _reopen(client, lab["report"], both)
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Authorization failures
# ---------------------------------------------------------------------------

class TestReopenAuthorizationFailures:
    def test_non_reviewer_pathologist_is_refused(self, client, session, lab):
        pathologist = create_user(
            session, lab["tenant"], email="path@t1.example", roles=("pathologist",)
        )
        before = _Unchanged(session, lab["report"])

        resp = _reopen(client, lab["report"], pathologist)
        assert resp.status_code == 403, resp.text
        before.assert_intact()

    def test_unassigned_reviewer_is_refused(self, client, session, lab):
        stranger = create_user(
            session, lab["tenant"], email="rev2@t1.example", roles=("reviewer",)
        )
        before = _Unchanged(session, lab["report"])

        resp = _reopen(client, lab["report"], stranger)
        assert resp.status_code == 403, resp.text
        before.assert_intact()

    def test_a_reviewer_assigned_to_a_different_order_is_refused(
        self, client, session, lab
    ):
        other_order = create_order(
            session, lab["tenant"], lab["branch"], order_code="ORD-2"
        )
        elsewhere = create_user(
            session, lab["tenant"], email="rev3@t1.example", roles=("reviewer",)
        )
        assign_reviewer(session, other_order, elsewhere)
        before = _Unchanged(session, lab["report"])

        resp = _reopen(client, lab["report"], elsewhere)
        assert resp.status_code == 403, resp.text
        before.assert_intact()

    def test_cross_tenant_actor_is_refused(self, client, session, lab):
        """An administrator of another laboratory holds
        `reports:manage_templates` in their OWN tenant. The tenant anchor runs
        before the policy, so the capability never reaches this report."""
        other_tenant = create_tenant(session, name="Other Lab")
        intruder = create_user(
            session, other_tenant, email="admin@t2.example", roles=("admin",)
        )
        before = _Unchanged(session, lab["report"])

        resp = _reopen(client, lab["report"], intruder)
        assert resp.status_code == 403, resp.text
        before.assert_intact()

    def test_cross_tenant_reviewer_is_refused(self, client, session, lab):
        other_tenant = create_tenant(session, name="Other Lab 2")
        intruder = create_user(
            session, other_tenant, email="rev@t2.example", roles=("reviewer",)
        )
        before = _Unchanged(session, lab["report"])

        resp = _reopen(client, lab["report"], intruder)
        assert resp.status_code == 403, resp.text
        before.assert_intact()

    def test_a_viewer_is_refused(self, client, session, lab):
        viewer = create_user(
            session, lab["tenant"], email="viewer@t1.example", roles=("viewer",)
        )
        before = _Unchanged(session, lab["report"])

        resp = _reopen(client, lab["report"], viewer)
        assert resp.status_code == 403, resp.text
        before.assert_intact()

    def test_an_unauthorized_caller_cannot_learn_the_status(
        self, client, session, lab
    ):
        """Authorization is evaluated before lifecycle state (the Block A
        ordering rule). A pathologist gets the same 403 whatever state the
        report is in, so the endpoint is not a state oracle."""
        pathologist = create_user(
            session, lab["tenant"], email="path@t1.example", roles=("pathologist",)
        )
        codes = set()
        for status in (
            ReportStatus.DRAFT,
            ReportStatus.IN_REVIEW,
            ReportStatus.APPROVED,
        ):
            _set_status(session, lab["report"], status)
            codes.add(_reopen(client, lab["report"], pathologist).status_code)
        assert codes == {403}


# ---------------------------------------------------------------------------
# Lifecycle failures
# ---------------------------------------------------------------------------

class TestReopenLifecycleFailures:
    @pytest.fixture
    def admin(self, session, lab):
        return create_user(
            session, lab["tenant"], email="admin@t1.example", roles=("admin",)
        )

    def test_a_draft_report_cannot_be_reopened(self, client, session, lab, admin):
        _set_status(session, lab["report"], ReportStatus.DRAFT)
        before = _Unchanged(session, lab["report"])

        resp = _reopen(client, lab["report"], admin)
        assert resp.status_code == 400, resp.text
        before.assert_intact()

    def test_an_in_review_report_cannot_be_reopened(
        self, client, session, lab, admin
    ):
        _set_status(session, lab["report"], ReportStatus.IN_REVIEW)
        before = _Unchanged(session, lab["report"])

        resp = _reopen(client, lab["report"], admin)
        assert resp.status_code == 400, resp.text
        before.assert_intact()

    def test_a_retracted_report_cannot_be_reopened(self, client, session, lab, admin):
        _set_status(session, lab["report"], ReportStatus.RETRACTED)
        before = _Unchanged(session, lab["report"])

        resp = _reopen(client, lab["report"], admin)
        assert resp.status_code == 400, resp.text
        before.assert_intact()

    def test_a_report_with_no_current_version_cannot_be_reopened(
        self, client, session, lab, admin
    ):
        version = session.get(ReportVersion, lab["version"].id)
        version.is_current = False
        session.add(version)
        session.commit()
        before = _Unchanged(session, lab["report"])

        resp = _reopen(client, lab["report"], admin)
        assert resp.status_code == 400, resp.text
        before.assert_intact()

    def test_an_unknown_report_is_404(self, client, session, lab, admin):
        resp = client.post(
            f"/api/v1/reports/{uuid.uuid4()}/reopen",
            json={},
            headers=auth_headers(admin),
        )
        assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# The signed / final hard stop
# ---------------------------------------------------------------------------

class TestSignedReportsAreNeverReopened:
    """The rule CEL-131-03 exists to keep: 1.3.1 reopens approved work, it
    does not amend signed work."""

    @pytest.fixture
    def admin(self, session, lab):
        return create_user(
            session, lab["tenant"], email="admin@t1.example", roles=("admin",)
        )

    def _sign(self, session, version, user):
        version = session.get(ReportVersion, version.id)
        version.signed_by = user.id
        version.signed_at = datetime.utcnow()
        session.add(version)
        session.commit()
        session.refresh(version)
        return version

    def test_approved_and_signed_is_refused(self, client, session, lab, admin):
        """The case the status check alone would miss. Signing writes
        `signed_at` on the version and only then moves the report to
        PUBLISHED; a report left APPROVED with a signature is still signed."""
        self._sign(session, lab["version"], lab["reviewer"])
        before = _Unchanged(session, lab["report"])

        resp = _reopen(client, lab["report"], admin)
        assert resp.status_code == 409, resp.text
        before.assert_intact()

    def test_the_assigned_reviewer_is_refused_too(self, client, session, lab):
        """Not an authorization question: nobody reopens a signed report."""
        self._sign(session, lab["version"], lab["reviewer"])
        before = _Unchanged(session, lab["report"])

        resp = _reopen(client, lab["report"], lab["reviewer"])
        assert resp.status_code == 409, resp.text
        before.assert_intact()

    def test_a_signature_on_an_older_version_is_enough_to_refuse(
        self, client, session, lab, admin
    ):
        """Every version is examined, not only the current one — a reopen must
        never resurrect a report any part of whose history was signed."""
        old = ReportVersion(
            report_id=lab["report"].id,
            version_no=0,
            is_current=False,
            signed_by=lab["reviewer"].id,
            signed_at=datetime.utcnow(),
        )
        session.add(old)
        session.commit()
        before = _Unchanged(session, lab["report"])

        resp = _reopen(client, lab["report"], admin)
        assert resp.status_code == 409, resp.text
        before.assert_intact()

    def test_signed_by_without_signed_at_is_enough_to_refuse(
        self, client, session, lab, admin
    ):
        version = session.get(ReportVersion, lab["version"].id)
        version.signed_by = lab["reviewer"].id
        session.add(version)
        session.commit()
        before = _Unchanged(session, lab["report"])

        resp = _reopen(client, lab["report"], admin)
        assert resp.status_code == 409, resp.text
        before.assert_intact()

    def test_a_published_at_without_a_signature_is_enough_to_refuse(
        self, client, session, lab, admin
    ):
        """`published_at` is the report-level record of a publication. The
        guard refuses on ANY finalisation evidence, not only the one the happy
        path writes."""
        report = session.get(Report, lab["report"].id)
        report.published_at = datetime.utcnow()
        session.add(report)
        session.commit()
        before = _Unchanged(session, lab["report"])

        resp = _reopen(client, lab["report"], admin)
        assert resp.status_code == 409, resp.text
        before.assert_intact()

    def test_a_published_report_is_refused(self, client, session, lab, admin):
        """The realistic shape of a signed report: sign-and-publish moves it
        straight past APPROVED, so the status guard refuses first — but the
        signature and the publication artifact must survive untouched either
        way."""
        self._sign(session, lab["version"], lab["reviewer"])
        report = session.get(Report, lab["report"].id)
        report.status = ReportStatus.PUBLISHED
        report.published_at = datetime.utcnow()
        session.add(report)
        session.commit()
        before = _Unchanged(session, lab["report"])

        resp = _reopen(client, lab["report"], admin)
        assert resp.status_code in (400, 409), resp.text
        before.assert_intact()

    def test_a_published_report_is_refused_for_the_reviewer_too(
        self, client, session, lab
    ):
        self._sign(session, lab["version"], lab["reviewer"])
        report = session.get(Report, lab["report"].id)
        report.status = ReportStatus.PUBLISHED
        report.published_at = datetime.utcnow()
        session.add(report)
        session.commit()
        before = _Unchanged(session, lab["report"])

        resp = _reopen(client, lab["report"], lab["reviewer"])
        assert resp.status_code in (400, 409), resp.text
        before.assert_intact()


# ---------------------------------------------------------------------------
# What a successful reopen does — and does not — change
# ---------------------------------------------------------------------------

class TestReopenEffects:
    def test_an_audit_event_is_written(self, client, session, lab):
        assert _reopen(client, lab["report"], lab["reviewer"]).status_code == 200

        rows = _audit(session, lab["report"], "REPORT.REOPEN")
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_user_id == lab["reviewer"].id
        assert str(row.tenant_id) == str(lab["tenant"].id)
        assert row.entity_type == "report"
        assert row.old_values["status"] == ReportStatus.APPROVED
        assert row.new_values["status"] == ReportStatus.DRAFT

    def test_the_audit_records_the_administrative_actor(self, client, session, lab):
        admin = create_user(
            session, lab["tenant"], email="admin@t1.example", roles=("admin",)
        )
        assert _reopen(client, lab["report"], admin).status_code == 200

        rows = _audit(session, lab["report"], "REPORT.REOPEN")
        assert len(rows) == 1
        assert rows[0].actor_user_id == admin.id

    def test_the_previous_review_decision_is_preserved(self, client, session, lab):
        """Historical evidence that the report WAS reviewed and approved is
        not erased. The reviewer's row keeps its decision and `decision_at`;
        the next submission is what starts a new review cycle."""
        decided_at = session.get(ReportReview, lab["review"].id).decision_at

        assert _reopen(client, lab["report"], lab["reviewer"]).status_code == 200

        session.expire_all()
        review = session.get(ReportReview, lab["review"].id)
        assert review.status == ReviewStatus.APPROVED
        assert review.decision_at == decided_at

    def test_the_approval_audit_row_is_not_rewritten(self, client, session, lab):
        session.add(
            AuditLog(
                tenant_id=lab["tenant"].id,
                branch_id=lab["branch"].id,
                actor_user_id=lab["reviewer"].id,
                action="REPORT.APPROVE",
                entity_type="report",
                entity_id=lab["report"].id,
                old_values={"status": "IN_REVIEW"},
                new_values={"status": "APPROVED"},
            )
        )
        session.commit()

        assert _reopen(client, lab["report"], lab["reviewer"]).status_code == 200

        assert len(_audit(session, lab["report"], "REPORT.APPROVE")) == 1

    def test_the_signature_fields_stay_empty(self, client, session, lab):
        assert _reopen(client, lab["report"], lab["reviewer"]).status_code == 200

        session.expire_all()
        version = session.get(ReportVersion, lab["version"].id)
        assert version.signed_at is None
        assert version.signed_by is None
        assert session.get(Report, lab["report"].id).published_at is None

    def test_the_current_version_is_not_replaced(self, client, session, lab):
        """Reopening is a status transition, not an edit. It must not create a
        version, and the frozen letterhead and PDF state ride with the version
        they belong to."""
        assert _reopen(client, lab["report"], lab["reviewer"]).status_code == 200

        session.expire_all()
        versions = session.exec(
            select(ReportVersion).where(ReportVersion.report_id == lab["report"].id)
        ).all()
        assert len(versions) == 1
        assert versions[0].id == lab["version"].id
        assert versions[0].is_current is True

    def test_an_already_reopened_report_cannot_be_reopened_again(
        self, client, session, lab
    ):
        assert _reopen(client, lab["report"], lab["reviewer"]).status_code == 200
        second = _reopen(client, lab["report"], lab["reviewer"])
        assert second.status_code == 400, second.text
        assert len(_audit(session, lab["report"], "REPORT.REOPEN")) == 1


# ---------------------------------------------------------------------------
# Re-approval is mandatory — the reopen must not leave a shortcut
# ---------------------------------------------------------------------------

class TestReopenedReportCannotSkipReview:
    def test_the_reviewer_cannot_sign_a_reopened_report(self, client, session, lab):
        assert _reopen(client, lab["report"], lab["reviewer"]).status_code == 200

        resp = client.post(
            f"/api/v1/reports/{lab['report'].id}/sign-and-publish",
            json={},
            headers=auth_headers(lab["reviewer"]),
        )
        assert resp.status_code == 400, resp.text

        session.expire_all()
        version = session.get(ReportVersion, lab["version"].id)
        assert version.signed_at is None
        assert session.get(Report, lab["report"].id).status == ReportStatus.DRAFT

    def test_the_reviewer_cannot_use_the_legacy_sign_route_either(
        self, client, session, lab
    ):
        assert _reopen(client, lab["report"], lab["reviewer"]).status_code == 200

        resp = client.post(
            f"/api/v1/reports/{lab['report'].id}/sign",
            json={},
            headers=auth_headers(lab["reviewer"]),
        )
        assert resp.status_code == 400, resp.text

    def test_the_reopener_does_not_gain_clinical_authority(
        self, client, session, lab
    ):
        """An administrator may reopen; that must not make them a reviewer.
        This is the A8 separation, exercised through the real transition
        rather than the predicate."""
        admin = create_user(
            session, lab["tenant"], email="admin@t1.example", roles=("admin",)
        )
        assert _reopen(client, lab["report"], admin).status_code == 200

        # DRAFT -> submit is the author's action and needs `reports:submit`,
        # which an administrator does not hold either.
        for path, body in (
            ("approve", {}),
            ("sign-and-publish", {}),
            ("request-changes", {"comment": "x"}),
        ):
            resp = client.post(
                f"/api/v1/reports/{lab['report'].id}/{path}",
                json=body,
                headers=auth_headers(admin),
            )
            assert resp.status_code == 403, f"{path}: {resp.text}"

        resp = client.patch(
            f"/api/v1/reports/{lab['report'].id}/presentation",
            json={"show_signature_section": True},
            headers=auth_headers(admin),
        )
        assert resp.status_code == 403, resp.text

    def test_a_reopened_report_does_not_reopen_the_presentation_window(
        self, client, session, lab
    ):
        """Block A keeps presentation settings reviewer-only and IN_REVIEW-only.
        Reopening returns the report to DRAFT — the reviewer's window comes
        back when it is resubmitted, not before."""
        assert _reopen(client, lab["report"], lab["reviewer"]).status_code == 200

        resp = client.patch(
            f"/api/v1/reports/{lab['report'].id}/presentation",
            json={"show_signature_section": True},
            headers=auth_headers(lab["reviewer"]),
        )
        assert resp.status_code == 409, resp.text


# ---------------------------------------------------------------------------
# The full round trip
# ---------------------------------------------------------------------------

class TestReopenedReportFollowsTheNormalLifecycle:
    def test_reopen_resubmit_reapprove(self, client, session, lab):
        """The acceptance criterion in one test: after a reopen the report
        travels DRAFT -> IN_REVIEW -> APPROVED again, through the ordinary
        routes and the ordinary actors, with a fresh review decision.

        It also pins the reason Block B does not touch `ReportReview` on
        reopen: `submit_report` already resets every reviewer of the order to
        PENDING, which is what creates the new review cycle.
        """
        author = create_user(
            session, lab["tenant"], email="author@t1.example", roles=("pathologist",)
        )

        assert _reopen(client, lab["report"], lab["reviewer"]).status_code == 200
        session.expire_all()
        assert session.get(Report, lab["report"].id).status == ReportStatus.DRAFT

        submit = client.post(
            f"/api/v1/reports/{lab['report'].id}/submit",
            json={},
            headers=auth_headers(author),
        )
        assert submit.status_code == 200, submit.text
        session.expire_all()
        assert session.get(Report, lab["report"].id).status == ReportStatus.IN_REVIEW
        # The previous decision is cleared by the submission, not by the
        # reopen — a new cycle on the same row.
        review = session.get(ReportReview, lab["review"].id)
        assert review.status == ReviewStatus.PENDING
        assert review.decision_at is None

        approve = client.post(
            f"/api/v1/reports/{lab['report'].id}/approve",
            json={},
            headers=auth_headers(lab["reviewer"]),
        )
        assert approve.status_code == 200, approve.text
        session.expire_all()
        assert session.get(Report, lab["report"].id).status == ReportStatus.APPROVED
        review = session.get(ReportReview, lab["review"].id)
        assert review.status == ReviewStatus.APPROVED
        assert review.decision_at is not None

    def test_the_report_can_be_reopened_again_after_being_reapproved(
        self, client, session, lab
    ):
        author = create_user(
            session, lab["tenant"], email="author@t1.example", roles=("pathologist",)
        )
        assert _reopen(client, lab["report"], lab["reviewer"]).status_code == 200
        assert (
            client.post(
                f"/api/v1/reports/{lab['report'].id}/submit",
                json={},
                headers=auth_headers(author),
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/v1/reports/{lab['report'].id}/approve",
                json={},
                headers=auth_headers(lab["reviewer"]),
            ).status_code
            == 200
        )

        assert _reopen(client, lab["report"], lab["reviewer"]).status_code == 200
        assert len(_audit(session, lab["report"], "REPORT.REOPEN")) == 2


# ---------------------------------------------------------------------------
# B-3 — the post-approval content-editing bypass
# ---------------------------------------------------------------------------

def _content(diagnosis: str = "Hallazgos"):
    return {
        "base": {"diagnosis": {"is_visible": True, "label": "Dx", "value": diagnosis}},
        "sections": {},
    }


def _new_version(client, lab, actor, *, diagnosis="Hallazgos actualizados"):
    return client.post(
        f"/api/v1/reports/{lab['report'].id}/new_version",
        json={
            "tenant_id": str(lab["tenant"].id),
            "branch_id": str(lab["branch"].id),
            "order_id": str(lab["order"].id),
            "report": _content(diagnosis),
        },
        headers=auth_headers(actor),
    )


def _versions(session: Session, report: Report):
    return session.exec(
        select(ReportVersion).where(ReportVersion.report_id == report.id)
    ).all()


class TestApprovedContentIsFrozen:
    """Finding B-3, remediated inside Block B.

    Phase 2's Story B9 froze content for PUBLISHED and RETRACTED and left
    APPROVED editable. That was coherent while approval was the last step
    before signing and there was no way back from it. Once Block B added
    `POST /{id}/reopen`, an editable APPROVED became a hole straight through
    the lifecycle it had just established: a holder of `reports:edit` could
    replace the clinical content of an approved report and have it signed
    without the new content ever being reviewed, leaving the approval
    attesting to text that no longer existed.

    The frontend already hid this (its save action is DRAFT-only), which is
    exactly why it survived to production: nothing exercised the API
    directly.
    """

    @pytest.fixture
    def author(self, session, lab):
        return create_user(
            session, lab["tenant"], email="author@t1.example", roles=("pathologist",)
        )

    def test_a_draft_report_accepts_a_new_version(self, client, session, lab, author):
        """The control: the guard must not have closed ordinary authoring."""
        _set_status(session, lab["report"], ReportStatus.DRAFT)

        resp = _new_version(client, lab, author)
        assert resp.status_code == 200, resp.text
        assert len(_versions(session, lab["report"])) == 2

    def test_an_in_review_report_still_accepts_a_new_version(
        self, client, session, lab, author
    ):
        """IN_REVIEW is deliberately still editable: nothing has been approved
        yet, so an edit there bypasses no decision — the reviewer's pending
        approval applies to whatever the content is when they make it."""
        _set_status(session, lab["report"], ReportStatus.IN_REVIEW)

        resp = _new_version(client, lab, author)
        assert resp.status_code == 200, resp.text

    def test_an_approved_report_refuses_a_new_version(
        self, client, session, lab, author
    ):
        """The bypass itself, end to end: the report is approved, the author
        holds `reports:edit`, and the API refuses."""
        before = _Unchanged(session, lab["report"])

        resp = _new_version(client, lab, author, diagnosis="Contenido inyectado")
        assert resp.status_code == 409, resp.text

        before.assert_intact()
        versions = _versions(session, lab["report"])
        assert len(versions) == 1, "a rejected edit created a version"
        assert versions[0].id == lab["version"].id
        assert versions[0].is_current is True

    def test_the_refusal_names_the_reopen_route(self, client, session, lab, author):
        resp = _new_version(client, lab, author)
        assert resp.status_code == 409
        assert "reábrelo" in resp.json()["detail"].lower()

    def test_a_published_report_remains_immutable(self, client, session, lab, author):
        version = session.get(ReportVersion, lab["version"].id)
        version.signed_by = lab["reviewer"].id
        version.signed_at = datetime.utcnow()
        session.add(version)
        report = session.get(Report, lab["report"].id)
        report.status = ReportStatus.PUBLISHED
        report.published_at = datetime.utcnow()
        session.add(report)
        session.commit()
        before = _Unchanged(session, lab["report"])

        resp = _new_version(client, lab, author)
        assert resp.status_code == 409, resp.text
        before.assert_intact()
        assert len(_versions(session, lab["report"])) == 1

    def test_a_retracted_report_remains_immutable(self, client, session, lab, author):
        _set_status(session, lab["report"], ReportStatus.RETRACTED)
        before = _Unchanged(session, lab["report"])

        resp = _new_version(client, lab, author)
        assert resp.status_code == 409, resp.text
        before.assert_intact()
        assert len(_versions(session, lab["report"])) == 1

    def test_the_permission_check_still_runs_first(self, client, session, lab):
        """A caller without `reports:edit` is refused with 403 whatever the
        report's state — the lifecycle guard must not have become a way to
        learn the status without permission."""
        reviewer = lab["reviewer"]
        for status in (
            ReportStatus.DRAFT,
            ReportStatus.APPROVED,
            ReportStatus.PUBLISHED,
        ):
            _set_status(session, lab["report"], status)
            resp = _new_version(client, lab, reviewer)
            assert resp.status_code == 403, f"{status}: {resp.text}"

    def test_a_cross_tenant_author_is_still_refused(self, client, session, lab):
        other_tenant = create_tenant(session, name="Other Lab 3")
        intruder = create_user(
            session, other_tenant, email="author@t2.example", roles=("pathologist",)
        )
        before = _Unchanged(session, lab["report"])

        resp = _new_version(client, lab, intruder)
        assert resp.status_code == 403, resp.text
        before.assert_intact()


class TestReopenIsTheOnlyWayBackToEditing:
    """The remediation and the transition are one contract: closing the
    bypass is only correct because reopening exists to replace it."""

    @pytest.fixture
    def author(self, session, lab):
        return create_user(
            session, lab["tenant"], email="author@t1.example", roles=("pathologist",)
        )

    def test_reopen_then_edit_then_the_full_cycle(self, client, session, lab, author):
        """The supported correction path, exercised end to end: the edit that
        was refused above succeeds once the report has been reopened, and the
        edited report still cannot be signed until it is reviewed and approved
        again."""
        assert _new_version(client, lab, author).status_code == 409

        assert _reopen(client, lab["report"], lab["reviewer"]).status_code == 200

        edit = _new_version(client, lab, author, diagnosis="Corregido")
        assert edit.status_code == 200, edit.text
        session.expire_all()
        assert len(_versions(session, lab["report"])) == 2

        # Still not signable: the new content has not been reviewed.
        blocked = client.post(
            f"/api/v1/reports/{lab['report'].id}/sign-and-publish",
            json={},
            headers=auth_headers(lab["reviewer"]),
        )
        assert blocked.status_code == 400, blocked.text

        assert (
            client.post(
                f"/api/v1/reports/{lab['report'].id}/submit",
                json={},
                headers=auth_headers(author),
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/v1/reports/{lab['report'].id}/approve",
                json={},
                headers=auth_headers(lab["reviewer"]),
            ).status_code
            == 200
        )
        session.expire_all()
        assert session.get(Report, lab["report"].id).status == ReportStatus.APPROVED

        # And the newly approved report is frozen again.
        assert _new_version(client, lab, author).status_code == 409

    def test_the_approved_content_survives_a_refused_edit(
        self, client, session, lab, author
    ):
        """What the approval attests to is exactly what stays on the report."""
        version = session.get(ReportVersion, lab["version"].id)
        original_id, original_no = version.id, version.version_no

        assert _new_version(client, lab, author, diagnosis="Otro").status_code == 409

        session.expire_all()
        current = session.exec(
            select(ReportVersion).where(
                ReportVersion.report_id == lab["report"].id,
                ReportVersion.is_current == True,  # noqa: E712
            )
        ).all()
        assert len(current) == 1
        assert current[0].id == original_id
        assert current[0].version_no == original_no
