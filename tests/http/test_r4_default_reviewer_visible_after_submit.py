"""Céluma 1.3.1 manual-validation remediation — R4 (CEL-131-06).

**The finding.** The tenant default reviewer could be configured and persisted
correctly, but "in the real workflow it does not appear to be applied".

**What reproduction actually showed.** The backend is correct. Driving the real
HTTP flow — configure the default through `PATCH /tenants/{id}`, create an
order with zero explicit reviewers, create a report, `POST /reports/{id}/submit`
— materializes exactly one PENDING `ReportReview`, and the fallback-assigned
reviewer can then exercise every reviewer action. The defect was in the
EDITOR's read model: `handleSubmit` applied the new status but never re-read
`/reports/{id}/full`, so the submitting session went on believing the order had
no reviewer until the page was reloaded by hand. Fixed in
`report_editor.tsx`; covered on the frontend by
`report_editor_r4_default_reviewer.test.tsx`.

**Why the existing suite allowed it through.** Block D's tests assert the
`ReportReview` ROW, by querying the table directly. Nothing asserted that the
assignment comes back through the endpoints the UI actually reads — which is
where "visible after reload" lives, and the exact gap between "the row exists"
and "the workflow shows it". This module closes that: every assertion here
goes through an HTTP read, never through the ORM.

**Scope note — R8 moved the primary materialization point.** A later
remediation changed the product contract: a valid tenant default reviewer is
now assigned at ORDER CREATION, and submission keeps the same helper as an
idempotent safety net. Every order here is built with the ORM factory rather
than through `POST /laboratory/orders/`, so it deliberately reaches submission
with no reviewer — which makes this module the SAFETY-NET coverage, still
exactly the path an order created before the release, or created while no
default was configured, takes today. The primary path is covered by
`test_r8_default_reviewer_at_order_creation.py`.
"""
import pytest
from sqlmodel import Session, select

from app.models.enums import ReportStatus, ReviewStatus
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

SUBMIT = "/api/v1/reports/{}/submit"
ORDER_FULL = "/api/v1/laboratory/orders/{}/full"
REPORT_FULL = "/api/v1/reports/{}/full"
TENANT = "/api/v1/tenants/{}"


def _configure_default(client, tenant, admin, reviewer_id):
    resp = client.patch(
        TENANT.format(tenant.id),
        json={"default_reviewer_id": str(reviewer_id)},
        headers=auth_headers(admin),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def lab(session: Session):
    """A tenant with an eligible reviewer configured as the default, and an
    order carrying a DRAFT report and ZERO explicit reviewer assignments."""
    tenant = create_tenant(session)
    branch = create_branch(session, tenant)
    order = create_order(session, tenant, branch)
    report, version = create_report(
        session, tenant, branch, order, status=ReportStatus.DRAFT
    )
    admin = create_user(session, tenant, email="admin@t1.example", roles=("admin",))
    reviewer = create_user(
        session,
        tenant,
        email="arisbeth@t1.example",
        roles=("reviewer",),
        full_name="Dra. Arisbeth Villanueva",
    )
    author = create_user(
        session, tenant, email="author@t1.example", roles=("pathologist",)
    )
    return {
        "tenant": tenant,
        "branch": branch,
        "order": order,
        "report": report,
        "version": version,
        "admin": admin,
        "reviewer": reviewer,
        "author": author,
    }


class TestTheAssignmentIsVisibleThroughTheEndpointsTheUiReads:
    def test_the_order_has_no_reviewer_before_submission(self, client, lab):
        """The premise of the SAFETY-NET scenario. If this ever stops holding,
        every assertion below is passing for the wrong reason.

        It holds because this module's orders are ORM-built. An order created
        through `POST /laboratory/orders/` while a valid default is configured
        gets its reviewer immediately (R8) and never reaches this state."""
        resp = client.get(
            ORDER_FULL.format(lab["order"].id), headers=auth_headers(lab["author"])
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["order"]["reviewers"] == []

    def test_the_default_reviewer_appears_on_the_order_after_submit(
        self, client, lab
    ):
        _configure_default(client, lab["tenant"], lab["admin"], lab["reviewer"].id)

        assert (
            client.post(
                SUBMIT.format(lab["report"].id),
                json={},
                headers=auth_headers(lab["author"]),
            ).status_code
            == 200
        )

        # "Reload": a fresh read, exactly what the order page does on mount.
        resp = client.get(
            ORDER_FULL.format(lab["order"].id), headers=auth_headers(lab["author"])
        )
        assert resp.status_code == 200, resp.text
        reviewers = resp.json()["order"]["reviewers"]
        assert len(reviewers) == 1
        assert reviewers[0]["id"] == str(lab["reviewer"].id)
        assert reviewers[0]["name"] == "Dra. Arisbeth Villanueva"
        assert reviewers[0]["status"] == "pending"

    def test_the_default_reviewer_appears_on_the_report_after_submit(
        self, client, lab
    ):
        """`GET /reports/{id}/full` is what the report editor reads, and
        `order.reviewers` from it is what every reviewer-contract flag in the
        editor is derived from. The R4 defect was this response never being
        re-read after submission."""
        _configure_default(client, lab["tenant"], lab["admin"], lab["reviewer"].id)
        client.post(
            SUBMIT.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["author"]),
        )

        resp = client.get(
            REPORT_FULL.format(lab["report"].id), headers=auth_headers(lab["author"])
        )
        assert resp.status_code == 200, resp.text
        reviewers = resp.json()["order"]["reviewers"]
        assert [r["id"] for r in reviewers] == [str(lab["reviewer"].id)]
        assert resp.json()["report"]["status"] == "IN_REVIEW"

    def test_the_fallback_reviewer_can_then_act_as_a_reviewer(self, client, lab):
        """A fallback assignment IS a real assignment — it satisfies the
        "assigned" half of Block A's double lock and nothing else."""
        _configure_default(client, lab["tenant"], lab["admin"], lab["reviewer"].id)
        client.post(
            SUBMIT.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["author"]),
        )

        resp = client.post(
            f"/api/v1/reports/{lab['report'].id}/approve",
            json={},
            headers=auth_headers(lab["reviewer"]),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "APPROVED"


class TestTheFallbackIsStillOnlyAFallback:
    def test_a_repeated_submit_never_creates_a_second_assignment(
        self, client, session, lab
    ):
        """Resubmission after `request-changes` (or Block B's reopen) must
        reuse the existing row — the partial unique index
        `ix_report_review_unique_pending` would otherwise be at risk, and the
        reviewer would appear twice in the UI."""
        _configure_default(client, lab["tenant"], lab["admin"], lab["reviewer"].id)
        client.post(
            SUBMIT.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["author"]),
        )

        client.post(
            f"/api/v1/reports/{lab['report'].id}/request-changes",
            json={"comment": "please revise"},
            headers=auth_headers(lab["reviewer"]),
        )
        resp = client.post(
            SUBMIT.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["author"]),
        )
        assert resp.status_code == 200, resp.text

        session.expire_all()
        rows = session.exec(
            select(ReportReview).where(ReportReview.order_id == lab["order"].id)
        ).all()
        assert len(rows) == 1
        assert rows[0].status == ReviewStatus.PENDING

        reviewers = client.get(
            ORDER_FULL.format(lab["order"].id), headers=auth_headers(lab["author"])
        ).json()["order"]["reviewers"]
        assert len(reviewers) == 1

    def test_an_explicit_assignment_wins_and_the_default_is_never_added(
        self, client, session, lab
    ):
        _configure_default(client, lab["tenant"], lab["admin"], lab["reviewer"].id)
        explicit = create_user(
            session, lab["tenant"], email="explicit@t1.example", roles=("reviewer",)
        )
        assign_reviewer(session, lab["order"], explicit, report=lab["report"])

        client.post(
            SUBMIT.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["author"]),
        )

        reviewers = client.get(
            ORDER_FULL.format(lab["order"].id), headers=auth_headers(lab["author"])
        ).json()["order"]["reviewers"]
        assert [r["id"] for r in reviewers] == [str(explicit.id)]

    def test_clearing_the_default_removes_the_fallback(self, client, lab):
        _configure_default(client, lab["tenant"], lab["admin"], lab["reviewer"].id)
        cleared = client.patch(
            TENANT.format(lab["tenant"].id),
            json={"clear_default_reviewer": True},
            headers=auth_headers(lab["admin"]),
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["default_reviewer_id"] is None

        resp = client.post(
            SUBMIT.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["author"]),
        )
        assert resp.status_code == 400, resp.text
        assert client.get(
            ORDER_FULL.format(lab["order"].id), headers=auth_headers(lab["author"])
        ).json()["order"]["reviewers"] == []

    def test_a_default_who_lost_the_reviewer_role_fails_closed(
        self, client, session, lab
    ):
        """Eligibility is re-derived live at submission, never trusted from
        configuration time. The report must refuse to submit rather than
        assign someone who no longer qualifies."""
        from app.models.role import Role
        from app.models.user_role import UserRoleLink

        _configure_default(client, lab["tenant"], lab["admin"], lab["reviewer"].id)

        role = session.exec(select(Role).where(Role.code == "reviewer")).first()
        link = session.exec(
            select(UserRoleLink).where(
                UserRoleLink.user_id == lab["reviewer"].id,
                UserRoleLink.role_id == role.id,
            )
        ).first()
        session.delete(link)
        session.commit()

        resp = client.post(
            SUBMIT.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["author"]),
        )
        assert resp.status_code == 400, resp.text
        assert client.get(
            ORDER_FULL.format(lab["order"].id), headers=auth_headers(lab["author"])
        ).json()["order"]["reviewers"] == []

    def test_a_deactivated_default_fails_closed(self, client, session, lab):
        _configure_default(client, lab["tenant"], lab["admin"], lab["reviewer"].id)
        reviewer = session.get(type(lab["reviewer"]), lab["reviewer"].id)
        reviewer.is_active = False
        session.add(reviewer)
        session.commit()

        resp = client.post(
            SUBMIT.format(lab["report"].id),
            json={},
            headers=auth_headers(lab["author"]),
        )
        assert resp.status_code == 400, resp.text
