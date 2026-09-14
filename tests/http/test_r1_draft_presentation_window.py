"""Céluma 1.3.1 manual-validation remediation — R1 (CEL-131-02).

**The finding.** CEL-131-02 worked, but an authorized assigned reviewer could
configure the reviewer-owned presentation settings only while the report was
IN_REVIEW. In the real workflow that is backwards: the reviewer owns the
letterhead and the signature toggles from the moment the report exists, and
Block A's own authorization contract (§7) already said so — it was only the
LIFECYCLE tuple that disagreed.

**Why the existing suite allowed it through.** It did not: the old behaviour
was asserted deliberately, by `test_a_draft_report_is_not_the_reviewers_window`
in `test_block_a_reviewer_authorization.py`. This was a product-contract
error, not a coding error, which is precisely the class of defect unit tests
cannot catch — the tests agreed with the code and both were wrong about what
the laboratory needed. That test is inverted in place, and this module adds
what it never covered: that opening DRAFT did not open anything ELSE.

**The property under test.** Widening
`PRESENTATION_EDITABLE_STATUSES` to `(DRAFT, IN_REVIEW)` must move exactly one
boundary. Every other lock — the reviewer role, the capability, the
assignment, the tenant, and above all the APPROVAL and SIGNING lifecycles —
must be observably unchanged. A reviewer who can now set a signature toggle in
DRAFT must still be unable to approve or sign from DRAFT, and an
administrator must still gain nothing.
"""
import uuid

import pytest
from sqlmodel import Session, select

from app.models.enums import ReportStatus, ReviewStatus
from app.models.report import Report, ReportVersion
from app.models.report_review import ReportReview

from .factories import (
    auth_headers,
    create_branch,
    create_order,
    create_tenant,
    create_user,
)
from .test_block_a_reviewer_authorization import (
    _assign,
    _read_body,
    _report,
    _store_body,
)

PRESENTATION = "/api/v1/reports/{}/presentation"
APPROVE = "/api/v1/reports/{}/approve"
SIGN = "/api/v1/reports/{}/sign"
SIGN_AND_PUBLISH = "/api/v1/reports/{}/sign-and-publish"

BODY = {
    "base": {"patient": "Doe, J."},
    "sections": [{"key": "micro", "text": "original clinical text"}],
    "signatureMetadata": {
        "show_signature_section": False,
        "require_digital_signature": False,
    },
}


def _lab(session: Session, *, status=ReportStatus.DRAFT, tenant_name="R1 Tenant"):
    tenant = create_tenant(session, name=tenant_name)
    branch = create_branch(session, tenant)
    order = create_order(session, tenant, branch, order_code=f"ORD-{uuid.uuid4().hex[:6]}")
    report, version = _report(session, tenant, branch, order, status=status)
    _store_body(session, version, tenant, dict(BODY))
    return {
        "tenant": tenant,
        "branch": branch,
        "order": order,
        "report": report,
        "version": version,
    }


def _set_status(session: Session, report: Report, status: ReportStatus):
    row = session.get(Report, report.id)
    row.status = status
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


# ---------------------------------------------------------------------------
# The widened window
# ---------------------------------------------------------------------------

class TestTheReviewerWindowNowIncludesDraft:
    @pytest.mark.parametrize(
        "status", [ReportStatus.DRAFT, ReportStatus.IN_REVIEW]
    )
    def test_the_assigned_reviewer_may_configure_presentation(
        self, client, session, status
    ):
        lab = _lab(session, status=status, tenant_name=f"R1 {status}")
        reviewer = create_user(
            session,
            lab["tenant"],
            email=f"rev-{uuid.uuid4().hex[:6]}@t1.example",
            roles=("reviewer",),
        )
        _assign(session, lab["report"], reviewer)

        resp = client.patch(
            PRESENTATION.format(lab["report"].id),
            json={"show_signature_section": True, "require_digital_signature": True},
            headers=auth_headers(reviewer),
        )

        assert resp.status_code == 200, resp.text
        body = _read_body(session, lab["version"])
        assert body["signatureMetadata"]["show_signature_section"] is True
        assert body["signatureMetadata"]["require_digital_signature"] is True

    def test_the_change_survives_the_authors_next_draft_content_save(
        self, client, session
    ):
        """The point of doing this in DRAFT at all.

        The author keeps writing after the reviewer sets the toggles, and the
        content path carries the PERSISTED presentation forward
        (`enforce_author_presentation_boundary`) — so the reviewer's decision
        must not be silently reverted by the next save. If this ever fails,
        DRAFT presentation editing is theatre.
        """
        lab = _lab(session)
        reviewer = create_user(
            session, lab["tenant"], email="rev@t1.example", roles=("reviewer",)
        )
        _assign(session, lab["report"], reviewer)
        author = create_user(
            session, lab["tenant"], email="path@t1.example", roles=("pathologist",)
        )

        client.patch(
            PRESENTATION.format(lab["report"].id),
            json={"show_signature_section": True, "require_digital_signature": True},
            headers=auth_headers(reviewer),
        )

        resp = client.post(
            f"/api/v1/reports/{lab['report'].id}/new_version",
            json={
                "tenant_id": str(lab["tenant"].id),
                "branch_id": str(lab["branch"].id),
                "order_id": str(lab["order"].id),
                "report": {
                    "base": {"patient": "Doe, J."},
                    "sections": [{"key": "micro", "text": "the author kept writing"}],
                    # The author's client round-trips the whole document and
                    # would happily send the OLD values back.
                    "signatureMetadata": {
                        "show_signature_section": False,
                        "require_digital_signature": False,
                    },
                },
            },
            headers=auth_headers(author),
        )
        assert resp.status_code in (200, 201), resp.text

        session.expire_all()
        current = session.exec(
            select(ReportVersion).where(
                ReportVersion.report_id == lab["report"].id,
                ReportVersion.is_current == True,  # noqa: E712
            )
        ).first()
        body = _read_body(session, current)
        assert body["signatureMetadata"]["show_signature_section"] is True
        assert body["signatureMetadata"]["require_digital_signature"] is True
        assert body["sections"] == [
            {"key": "micro", "text": "the author kept writing"}
        ]


# ---------------------------------------------------------------------------
# Everything the widening must NOT have moved
# ---------------------------------------------------------------------------

class TestTheDoubleLockIsUnchangedInDraft:
    def test_a_pathologist_cannot_configure_presentation_in_draft(
        self, client, session
    ):
        lab = _lab(session)
        patho = create_user(
            session, lab["tenant"], email="path@t1.example", roles=("pathologist",)
        )

        resp = client.patch(
            PRESENTATION.format(lab["report"].id),
            json={"show_signature_section": True},
            headers=auth_headers(patho),
        )
        assert resp.status_code == 403, resp.text
        assert _read_body(session, lab["version"])["signatureMetadata"][
            "show_signature_section"
        ] is False

    @pytest.mark.parametrize("role", ["admin", "superuser"])
    def test_an_administrator_gains_nothing_in_draft(self, client, session, role):
        """Administrative privilege never confers clinical authority — the
        rule CEL-131-01 exists to enforce. `superuser` holds the entire
        permission catalogue, including `reports:approve`, and is still
        refused because it is not a reviewer assigned to this order."""
        lab = _lab(session, tenant_name=f"R1 {role}")
        admin = create_user(
            session, lab["tenant"], email=f"{role}@t1.example", roles=(role,)
        )

        resp = client.patch(
            PRESENTATION.format(lab["report"].id),
            json={"show_signature_section": True},
            headers=auth_headers(admin),
        )
        assert resp.status_code == 403, resp.text

    def test_a_reviewer_who_is_not_assigned_is_refused_in_draft(
        self, client, session
    ):
        lab = _lab(session)
        stranger = create_user(
            session, lab["tenant"], email="other-rev@t1.example", roles=("reviewer",)
        )

        resp = client.patch(
            PRESENTATION.format(lab["report"].id),
            json={"show_signature_section": True},
            headers=auth_headers(stranger),
        )
        assert resp.status_code == 403, resp.text

    def test_a_reviewer_from_another_tenant_is_refused_in_draft(
        self, client, session
    ):
        """404, not 403: a foreign report id is never confirmed to exist.
        Block A §5."""
        lab = _lab(session)
        other_tenant = create_tenant(session, name="Another Laboratory")
        other_branch = create_branch(session, other_tenant, code="OTHER")
        foreign = create_user(
            session, other_tenant, email="rev@t2.example", roles=("reviewer",)
        )
        assert other_branch is not None

        resp = client.patch(
            PRESENTATION.format(lab["report"].id),
            json={"show_signature_section": True},
            headers=auth_headers(foreign),
        )
        assert resp.status_code == 404, resp.text

    def test_a_reviewer_still_has_no_content_path_in_draft(self, client, session):
        """The reviewer must not have acquired `reports:edit` as a side effect
        of gaining a DRAFT window."""
        lab = _lab(session)
        reviewer = create_user(
            session, lab["tenant"], email="rev@t1.example", roles=("reviewer",)
        )
        _assign(session, lab["report"], reviewer)

        resp = client.post(
            f"/api/v1/reports/{lab['report'].id}/new_version",
            json={
                "tenant_id": str(lab["tenant"].id),
                "branch_id": str(lab["branch"].id),
                "order_id": str(lab["order"].id),
                "report": {"sections": [{"key": "micro", "text": "rewritten"}]},
            },
            headers=auth_headers(reviewer),
        )
        assert resp.status_code == 403, resp.text
        assert "reports:edit" in resp.text


class TestFrozenStatesStayFrozen:
    @pytest.mark.parametrize(
        "status",
        [ReportStatus.APPROVED, ReportStatus.PUBLISHED, ReportStatus.RETRACTED],
    )
    def test_presentation_is_refused_even_for_the_assigned_reviewer(
        self, client, session, status
    ):
        lab = _lab(session, tenant_name=f"R1 frozen {status}")
        reviewer = create_user(
            session,
            lab["tenant"],
            email=f"rev-{uuid.uuid4().hex[:6]}@t1.example",
            roles=("reviewer",),
        )
        _assign(session, lab["report"], reviewer, status=ReviewStatus.APPROVED)
        _set_status(session, lab["report"], status)

        resp = client.patch(
            PRESENTATION.format(lab["report"].id),
            json={"show_signature_section": True},
            headers=auth_headers(reviewer),
        )
        assert resp.status_code == 409, resp.text
        assert _read_body(session, lab["version"])["signatureMetadata"][
            "show_signature_section"
        ] is False


class TestApprovalAndSigningRemainClosedInDraft:
    """The security half of R1, and the reason the prompt insisted on the
    distinction: "editing reviewer-owned presentation configuration" is not
    "approving" and is not "signing". Those read their own lifecycle tuples,
    and neither admits DRAFT."""

    def test_the_assigned_reviewer_cannot_approve_from_draft(
        self, client, session
    ):
        lab = _lab(session)
        reviewer = create_user(
            session, lab["tenant"], email="rev@t1.example", roles=("reviewer",)
        )
        _assign(session, lab["report"], reviewer)

        # ...even after exercising the newly granted presentation window, so
        # this cannot pass merely because the reviewer never touched anything.
        assert client.patch(
            PRESENTATION.format(lab["report"].id),
            json={"show_signature_section": True},
            headers=auth_headers(reviewer),
        ).status_code == 200

        resp = client.post(
            APPROVE.format(lab["report"].id), json={}, headers=auth_headers(reviewer)
        )
        assert resp.status_code == 400, resp.text
        session.expire_all()
        assert session.get(Report, lab["report"].id).status == ReportStatus.DRAFT
        review = session.exec(
            select(ReportReview).where(ReportReview.order_id == lab["order"].id)
        ).first()
        assert review.status == ReviewStatus.PENDING
        assert review.decision_at is None

    @pytest.mark.parametrize("endpoint", [SIGN, SIGN_AND_PUBLISH])
    def test_the_assigned_reviewer_cannot_sign_from_draft(
        self, client, session, endpoint
    ):
        lab = _lab(session, tenant_name=f"R1 sign {endpoint}")
        reviewer = create_user(
            session,
            lab["tenant"],
            email=f"rev-{uuid.uuid4().hex[:6]}@t1.example",
            roles=("reviewer",),
        )
        _assign(session, lab["report"], reviewer)

        assert client.patch(
            PRESENTATION.format(lab["report"].id),
            json={"show_signature_section": True},
            headers=auth_headers(reviewer),
        ).status_code == 200

        resp = client.post(
            endpoint.format(lab["report"].id), json={}, headers=auth_headers(reviewer)
        )
        assert resp.status_code == 400, resp.text

        session.expire_all()
        report = session.get(Report, lab["report"].id)
        version = session.get(ReportVersion, lab["version"].id)
        assert report.status == ReportStatus.DRAFT
        assert report.published_at is None
        assert version.signed_at is None
        assert version.signed_by is None
