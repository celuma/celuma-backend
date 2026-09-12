"""HTTP integration tests for Céluma 1.3.1 Block D / CEL-131-06 — the
tenant-level default reviewer.

`Tenant.default_reviewer_id` is a CONFIGURATION reference, exposed through
the existing `PATCH /api/v1/tenants/{id}` (same `admin:manage_tenant` gate as
`reports_v2_enabled` — see `test_tenant_reports_v2_toggle.py`), and resolved
into a real `ReportReview` assignment by
`app/services/report_default_reviewer.py::resolve_fallback_reviewer_assignment`,
called from `submit_report` ONLY when the order has no reviewer at all. See
docs/celuma-1.3.1/block-d/default-reviewer-contract.md for the full contract
this module exercises.
"""
from app.models.enums import ReportStatus, ReviewStatus
from app.models.report_review import ReportReview
from app.models.tenant import Tenant

from .factories import (
    assign_reviewer,
    auth_headers,
    create_branch,
    create_order,
    create_report,
    create_tenant,
    create_user,
)


def _configure_default_reviewer(client, tenant, actor, reviewer_id):
    return client.patch(
        f"/api/v1/tenants/{tenant.id}",
        json={"default_reviewer_id": str(reviewer_id)},
        headers=auth_headers(actor),
    )


def _clear_default_reviewer(client, tenant, actor):
    return client.patch(
        f"/api/v1/tenants/{tenant.id}",
        json={"clear_default_reviewer": True},
        headers=auth_headers(actor),
    )


# ---------------------------------------------------------------------------
# 1/2 — store and clear the tenant configuration
# ---------------------------------------------------------------------------

class TestConfiguringTheDefault:
    def test_tenant_can_store_an_eligible_default_reviewer(self, client, session):
        tenant = create_tenant(session)
        admin = create_user(session, tenant, email="admin@t1.example", roles=("admin",))
        reviewer = create_user(session, tenant, email="rev@t1.example", roles=("reviewer",))

        resp = _configure_default_reviewer(client, tenant, admin, reviewer.id)
        assert resp.status_code == 200, resp.text
        assert resp.json()["default_reviewer_id"] == str(reviewer.id)
        assert resp.json()["default_reviewer"]["id"] == str(reviewer.id)

        session.expire_all()
        assert session.get(Tenant, tenant.id).default_reviewer_id == reviewer.id

    def test_tenant_can_clear_the_default(self, client, session):
        tenant = create_tenant(session)
        admin = create_user(session, tenant, email="admin@t1.example", roles=("admin",))
        reviewer = create_user(session, tenant, email="rev@t1.example", roles=("reviewer",))
        _configure_default_reviewer(client, tenant, admin, reviewer.id)

        resp = _clear_default_reviewer(client, tenant, admin)
        assert resp.status_code == 200, resp.text
        assert resp.json()["default_reviewer_id"] is None
        assert resp.json()["default_reviewer"] is None

        session.expire_all()
        assert session.get(Tenant, tenant.id).default_reviewer_id is None

    def test_no_configured_default_remains_a_valid_tenant_state(self, client, session):
        """A laboratory that never configures a default is not a broken or
        degraded tenant — it is the ordinary state, distinguishable from a
        failed request only by the 200 and a null field."""
        tenant = create_tenant(session)
        admin = create_user(session, tenant, email="admin@t1.example", roles=("admin",))

        resp = client.get(f"/api/v1/tenants/{tenant.id}", headers=auth_headers(admin))
        assert resp.status_code == 200, resp.text
        assert resp.json()["default_reviewer_id"] is None
        assert resp.json()["default_reviewer"] is None


# ---------------------------------------------------------------------------
# 3/4/5 — eligibility is enforced at configuration time
# ---------------------------------------------------------------------------

class TestEligibilityAtConfigurationTime:
    def test_non_reviewer_cannot_be_configured(self, client, session):
        tenant = create_tenant(session)
        admin = create_user(session, tenant, email="admin@t1.example", roles=("admin",))
        pathologist = create_user(
            session, tenant, email="path@t1.example", roles=("pathologist",)
        )

        resp = _configure_default_reviewer(client, tenant, admin, pathologist.id)
        assert resp.status_code == 422, resp.text

        session.expire_all()
        assert session.get(Tenant, tenant.id).default_reviewer_id is None

    def test_inactive_reviewer_cannot_be_configured(self, client, session):
        tenant = create_tenant(session)
        admin = create_user(session, tenant, email="admin@t1.example", roles=("admin",))
        reviewer = create_user(session, tenant, email="rev@t1.example", roles=("reviewer",))
        reviewer.is_active = False
        session.add(reviewer)
        session.commit()

        resp = _configure_default_reviewer(client, tenant, admin, reviewer.id)
        assert resp.status_code == 422, resp.text

        session.expire_all()
        assert session.get(Tenant, tenant.id).default_reviewer_id is None

    def test_other_tenant_reviewer_cannot_be_configured(self, client, session):
        tenant = create_tenant(session)
        other_tenant = create_tenant(session, name="Other Lab")
        admin = create_user(session, tenant, email="admin@t1.example", roles=("admin",))
        foreign_reviewer = create_user(
            session, other_tenant, email="rev@t2.example", roles=("reviewer",)
        )

        resp = _configure_default_reviewer(client, tenant, admin, foreign_reviewer.id)
        assert resp.status_code == 400, resp.text

        session.expire_all()
        assert session.get(Tenant, tenant.id).default_reviewer_id is None

    def test_unauthorized_user_cannot_modify_the_setting(self, client, session):
        tenant = create_tenant(session)
        pathologist = create_user(
            session, tenant, email="path@t1.example", roles=("pathologist",)
        )
        reviewer = create_user(session, tenant, email="rev@t1.example", roles=("reviewer",))

        resp = _configure_default_reviewer(client, tenant, pathologist, reviewer.id)
        assert resp.status_code == 403, resp.text

        session.expire_all()
        assert session.get(Tenant, tenant.id).default_reviewer_id is None


# ---------------------------------------------------------------------------
# 7/8 — no privilege escalation
# ---------------------------------------------------------------------------

class TestNoPrivilegeEscalation:
    def test_admin_does_not_gain_reviewer_authority_by_configuring(
        self, client, session
    ):
        """The configuring actor is never implicitly the default, and
        configuring grants them nothing over reports they do not otherwise
        have a reviewer relationship to."""
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        admin = create_user(session, tenant, email="admin@t1.example", roles=("admin",))
        reviewer = create_user(session, tenant, email="rev@t1.example", roles=("reviewer",))
        _configure_default_reviewer(client, tenant, admin, reviewer.id)
        report, _ = create_report(
            session, tenant, branch, order, status=ReportStatus.IN_REVIEW
        )
        assign_reviewer(session, order, reviewer, report=report)

        resp = client.post(
            f"/api/v1/reports/{report.id}/approve",
            json={},
            headers=auth_headers(admin),
        )
        assert resp.status_code == 403, resp.text

    def test_configured_reviewer_receives_no_new_role_or_permission(
        self, client, session
    ):
        from app.core.rbac import get_user_permissions, get_user_roles

        tenant = create_tenant(session)
        admin = create_user(session, tenant, email="admin@t1.example", roles=("admin",))
        reviewer = create_user(session, tenant, email="rev@t1.example", roles=("reviewer",))
        roles_before = set(get_user_roles(reviewer.id, session))
        permissions_before = set(get_user_permissions(reviewer.id, session))

        resp = _configure_default_reviewer(client, tenant, admin, reviewer.id)
        assert resp.status_code == 200, resp.text

        session.expire_all()
        assert set(get_user_roles(reviewer.id, session)) == roles_before
        assert set(get_user_permissions(reviewer.id, session)) == permissions_before


# ---------------------------------------------------------------------------
# The fallback assignment at submit_report
# ---------------------------------------------------------------------------

class TestFallbackAtSubmission:
    def _lab(self, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        report, version = create_report(session, tenant, branch, order)
        author = create_user(
            session, tenant, email="author@t1.example", roles=("pathologist",)
        )
        return tenant, branch, order, report, author

    def test_default_reviewer_is_assigned_when_the_order_has_none(
        self, client, session
    ):
        tenant, branch, order, report, author = self._lab(session)
        admin = create_user(session, tenant, email="admin@t1.example", roles=("admin",))
        reviewer = create_user(session, tenant, email="rev@t1.example", roles=("reviewer",))
        _configure_default_reviewer(client, tenant, admin, reviewer.id)

        resp = client.post(
            f"/api/v1/reports/{report.id}/submit", json={}, headers=auth_headers(author)
        )
        assert resp.status_code == 200, resp.text

        session.expire_all()
        from sqlmodel import select

        reviews = session.exec(
            select(ReportReview).where(ReportReview.order_id == order.id)
        ).all()
        assert len(reviews) == 1
        assert reviews[0].reviewer_user_id == reviewer.id
        assert reviews[0].status == ReviewStatus.PENDING
        assert reviews[0].report_id == report.id
        assert reviews[0].assigned_by_user_id is None

    def test_no_default_configured_still_400s_as_before(self, client, session):
        tenant, branch, order, report, author = self._lab(session)

        resp = client.post(
            f"/api/v1/reports/{report.id}/submit", json={}, headers=auth_headers(author)
        )
        assert resp.status_code == 400, resp.text

    def test_explicit_assignment_is_never_overwritten_by_the_default(
        self, client, session
    ):
        tenant, branch, order, report, author = self._lab(session)
        admin = create_user(session, tenant, email="admin@t1.example", roles=("admin",))
        default_reviewer = create_user(
            session, tenant, email="default@t1.example", roles=("reviewer",)
        )
        explicit_reviewer = create_user(
            session, tenant, email="explicit@t1.example", roles=("reviewer",)
        )
        _configure_default_reviewer(client, tenant, admin, default_reviewer.id)
        assign_reviewer(session, order, explicit_reviewer, report=report)

        resp = client.post(
            f"/api/v1/reports/{report.id}/submit", json={}, headers=auth_headers(author)
        )
        assert resp.status_code == 200, resp.text

        session.expire_all()
        from sqlmodel import select

        reviews = session.exec(
            select(ReportReview).where(ReportReview.order_id == order.id)
        ).all()
        assert len(reviews) == 1
        assert reviews[0].reviewer_user_id == explicit_reviewer.id

    def test_stale_default_after_role_removal_is_not_used(self, client, session):
        from sqlmodel import select

        from app.core.rbac import ROLE_REVIEWER
        from app.models.role import Role
        from app.models.user_role import UserRoleLink

        tenant, branch, order, report, author = self._lab(session)
        admin = create_user(session, tenant, email="admin@t1.example", roles=("admin",))
        reviewer = create_user(session, tenant, email="rev@t1.example", roles=("reviewer",))
        _configure_default_reviewer(client, tenant, admin, reviewer.id)

        reviewer_role = session.exec(
            select(Role).where(Role.code == ROLE_REVIEWER)
        ).one()
        link = session.exec(
            select(UserRoleLink).where(
                UserRoleLink.user_id == reviewer.id,
                UserRoleLink.role_id == reviewer_role.id,
            )
        ).one()
        session.delete(link)
        session.commit()

        resp = client.post(
            f"/api/v1/reports/{report.id}/submit", json={}, headers=auth_headers(author)
        )
        assert resp.status_code == 400, resp.text

        session.expire_all()
        from sqlmodel import select

        assert (
            session.exec(
                select(ReportReview).where(ReportReview.order_id == order.id)
            ).first()
            is None
        )

    def test_stale_default_after_deactivation_is_not_used(self, client, session):
        tenant, branch, order, report, author = self._lab(session)
        admin = create_user(session, tenant, email="admin@t1.example", roles=("admin",))
        reviewer = create_user(session, tenant, email="rev@t1.example", roles=("reviewer",))
        _configure_default_reviewer(client, tenant, admin, reviewer.id)
        reviewer.is_active = False
        session.add(reviewer)
        session.commit()

        resp = client.post(
            f"/api/v1/reports/{report.id}/submit", json={}, headers=auth_headers(author)
        )
        assert resp.status_code == 400, resp.text

    def test_tenant_isolation_is_enforced_even_if_the_column_were_forged(
        self, session
    ):
        """Backend-side, not only through the configuration endpoint's own
        validation: even a row that somehow pointed cross-tenant is refused
        by the resolver itself."""
        from app.services.report_default_reviewer import (
            resolve_fallback_reviewer_assignment,
        )

        tenant = create_tenant(session)
        other_tenant = create_tenant(session, name="Other Lab")
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        report, _ = create_report(session, tenant, branch, order)
        foreign_reviewer = create_user(
            session, other_tenant, email="rev@t2.example", roles=("reviewer",)
        )
        tenant.default_reviewer_id = foreign_reviewer.id
        session.add(tenant)
        session.commit()

        assert resolve_fallback_reviewer_assignment(session, report) is None


# ---------------------------------------------------------------------------
# Block A / Block B invariants are unaffected by a fallback-created assignment
# ---------------------------------------------------------------------------

class TestBlockAAndBInvariantsHoldForTheFallback:
    def _submitted_via_fallback(self, client, session):
        tenant = create_tenant(session)
        branch = create_branch(session, tenant)
        order = create_order(session, tenant, branch)
        report, _ = create_report(session, tenant, branch, order)
        author = create_user(
            session, tenant, email="author@t1.example", roles=("pathologist",)
        )
        admin = create_user(session, tenant, email="admin@t1.example", roles=("admin",))
        reviewer = create_user(session, tenant, email="rev@t1.example", roles=("reviewer",))
        _configure_default_reviewer(client, tenant, admin, reviewer.id)
        client.post(
            f"/api/v1/reports/{report.id}/submit", json={}, headers=auth_headers(author)
        )
        return tenant, order, report, reviewer, author

    def test_approval_still_requires_the_fallback_reviewer_specifically(
        self, client, session
    ):
        tenant, order, report, reviewer, author = self._submitted_via_fallback(
            client, session
        )
        stranger = create_user(
            session, tenant, email="stranger@t1.example", roles=("reviewer",)
        )

        resp = client.post(
            f"/api/v1/reports/{report.id}/approve",
            json={},
            headers=auth_headers(stranger),
        )
        assert resp.status_code == 403, resp.text

        resp = client.post(
            f"/api/v1/reports/{report.id}/approve",
            json={},
            headers=auth_headers(reviewer),
        )
        assert resp.status_code == 200, resp.text

    def test_reopen_does_not_reassign_or_duplicate_the_reviewer(
        self, client, session
    ):
        """Reopening, resubmitting, must not touch `report_review` at all —
        the existing Block B contract — so the order never accumulates a
        second fallback row on a later submission."""
        from sqlmodel import select

        tenant, order, report, reviewer, author = self._submitted_via_fallback(
            client, session
        )
        approve = client.post(
            f"/api/v1/reports/{report.id}/approve",
            json={},
            headers=auth_headers(reviewer),
        )
        assert approve.status_code == 200, approve.text

        reopen = client.post(
            f"/api/v1/reports/{report.id}/reopen",
            json={},
            headers=auth_headers(reviewer),
        )
        assert reopen.status_code == 200, reopen.text

        resubmit = client.post(
            f"/api/v1/reports/{report.id}/submit", json={}, headers=auth_headers(author)
        )
        assert resubmit.status_code == 200, resubmit.text

        session.expire_all()
        reviews = session.exec(
            select(ReportReview).where(ReportReview.order_id == order.id)
        ).all()
        assert len(reviews) == 1
        assert reviews[0].reviewer_user_id == reviewer.id
        assert reviews[0].status == ReviewStatus.PENDING
