"""Céluma 1.3.1 manual-validation remediation — R8 (CEL-131-06).

**The product contract changed.** Block D materialized the tenant's default
reviewer at ONE moment, report submission, because that is where the existing
"cannot submit without reviewers" gate already lived
(`default-reviewer-contract.md` §4, option C). Manual validation rejected that
as the primary moment, for a reason Block D could not have anticipated: R1
later allowed an ASSIGNED reviewer to configure a report's presentation while
it is still a DRAFT, and a laboratory whose reviewer is configured as the
tenant default could not use that at all — no assignment existed until
submission. The order also read "Sin revisores asignados" for its entire
authoring phase.

The contract is now:

    a valid tenant default reviewer is assigned to the order AT ORDER
    CREATION; submission keeps the same helper as an idempotent safety net.

**Why the previous reproduction did not find this.** It was not a defect at
all under the old contract — the read models were consistent with it. Driving
the real flow showed the default reviewer being refused
(`403 You are not the assigned reviewer for this report`) on a DRAFT and the
order correctly reporting no reviewers. The old tests asserted exactly that
and passed. Only a product decision could change it, which is what happened.

**What these tests pin.** One implementation
(`ensure_default_reviewer_assignment`), reached from two call sites, that is
idempotent by construction; a default that never blocks order creation; and an
assignment indistinguishable from a human's for authorization purposes.
"""
import uuid

import pytest
from sqlmodel import Session, select

from app.models.enums import ReportStatus, ReviewStatus
from app.models.report_review import ReportReview
from app.models.role import Role
from app.models.study_type import StudyType
from app.models.user_role import UserRoleLink

from .factories import (
    auth_headers,
    create_branch,
    create_patient,
    create_tenant,
    create_user,
)
from .test_block_a_reviewer_authorization import _store_body

ORDERS = "/api/v1/laboratory/orders/"
ORDER_FULL = "/api/v1/laboratory/orders/{}/full"
TENANT = "/api/v1/tenants/{}"
SUBMIT = "/api/v1/reports/{}/submit"
PRESENTATION = "/api/v1/reports/{}/presentation"


def _create_study_type(session: Session, tenant, *, code="HIST") -> StudyType:
    study_type = StudyType(tenant_id=tenant.id, code=code, name="Histopatología")
    session.add(study_type)
    session.commit()
    session.refresh(study_type)
    return study_type


@pytest.fixture
def lab(session: Session):
    """A tenant with an admin who can create orders, an eligible reviewer, and
    a pathologist. The default reviewer is NOT configured yet — each test
    decides, because "was a valid default configured at creation time" is the
    whole variable."""
    tenant = create_tenant(session)
    branch = create_branch(session, tenant)
    study_type = _create_study_type(session, tenant)
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
    # `create_order` requires a patient or a requesting physician.
    patient = create_patient(session, tenant, branch)
    return {
        "patient": patient,
        "tenant": tenant,
        "branch": branch,
        "study_type": study_type,
        "admin": admin,
        "reviewer": reviewer,
        "author": author,
    }


def _configure_default(client, lab, reviewer_id, *, expect=200):
    resp = client.patch(
        TENANT.format(lab["tenant"].id),
        json={"default_reviewer_id": str(reviewer_id)},
        headers=auth_headers(lab["admin"]),
    )
    assert resp.status_code == expect, resp.text
    return resp


def _create_order(client, lab, **overrides):
    payload = {
        "tenant_id": str(lab["tenant"].id),
        "branch_id": str(lab["branch"].id),
        "study_type_id": str(lab["study_type"].id),
        "patient_id": str(lab["patient"].id),
    }
    payload.update(overrides)
    resp = client.post(ORDERS, json=payload, headers=auth_headers(lab["admin"]))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _reviews(session: Session, order_id):
    session.expire_all()
    return session.exec(
        select(ReportReview).where(ReportReview.order_id == uuid.UUID(str(order_id)))
    ).all()


def _reviewers_via_api(client, lab, order_id, *, as_user=None):
    resp = client.get(
        ORDER_FULL.format(order_id),
        headers=auth_headers(as_user or lab["admin"]),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["order"]["reviewers"]


# ---------------------------------------------------------------------------
# The primary path
# ---------------------------------------------------------------------------

class TestMaterializationAtOrderCreation:
    def test_a_valid_default_becomes_one_pending_assignment(
        self, client, session, lab
    ):
        _configure_default(client, lab, lab["reviewer"].id)

        order = _create_order(client, lab)

        rows = _reviews(session, order["id"])
        assert len(rows) == 1
        assert rows[0].reviewer_user_id == lab["reviewer"].id
        assert rows[0].status == ReviewStatus.PENDING
        assert rows[0].decision_at is None
        # No human performed this assignment.
        assert rows[0].assigned_by_user_id is None
        # No report exists yet — `report_review.report_id` is nullable for
        # exactly this, and authorization matches on `order_id`.
        assert rows[0].report_id is None

    def test_the_reviewer_is_visible_immediately_through_the_read_model(
        self, client, lab
    ):
        """The invariant the remediation asks for: if a reviewer is effective
        for the order, every read model exposes them immediately — not after
        submission, and not after approval."""
        _configure_default(client, lab, lab["reviewer"].id)

        order = _create_order(client, lab)

        reviewers = _reviewers_via_api(client, lab, order["id"])
        assert len(reviewers) == 1
        assert reviewers[0]["id"] == str(lab["reviewer"].id)
        assert reviewers[0]["name"] == "Dra. Arisbeth Villanueva"
        assert reviewers[0]["status"] == "pending"

    def test_a_report_created_afterwards_sees_the_reviewer_as_assigned(
        self, client, session, lab
    ):
        _configure_default(client, lab, lab["reviewer"].id)
        order = _create_order(client, lab)

        report = _draft_report(session, lab, order["id"])

        resp = client.get(
            f"/api/v1/reports/{report.id}/full", headers=auth_headers(lab["author"])
        )
        assert resp.status_code == 200, resp.text
        assert [r["id"] for r in resp.json()["order"]["reviewers"]] == [
            str(lab["reviewer"].id)
        ]

    def test_the_reviewer_can_use_draft_presentation_controls_under_r1(
        self, client, session, lab
    ):
        """The reason the moment moved. R1 gives an ASSIGNED reviewer the
        presentation window in DRAFT; before this change the tenant default
        was not assigned yet, so a laboratory configured that way was refused
        `403 You are not the assigned reviewer for this report`."""
        _configure_default(client, lab, lab["reviewer"].id)
        order = _create_order(client, lab)
        report = _draft_report(session, lab, order["id"])

        resp = client.patch(
            PRESENTATION.format(report.id),
            json={"show_signature_section": True, "require_digital_signature": True},
            headers=auth_headers(lab["reviewer"]),
        )
        assert resp.status_code == 200, resp.text
        assert session.get(type(report), report.id).status == ReportStatus.DRAFT


# ---------------------------------------------------------------------------
# Idempotence — one implementation, two call sites, never a duplicate
# ---------------------------------------------------------------------------

class TestIdempotence:
    def test_submitting_keeps_the_same_assignment_and_adds_nothing(
        self, client, session, lab
    ):
        _configure_default(client, lab, lab["reviewer"].id)
        order = _create_order(client, lab)
        original = _reviews(session, order["id"])[0]
        report = _draft_report(session, lab, order["id"])

        resp = client.post(
            SUBMIT.format(report.id), json={}, headers=auth_headers(lab["author"])
        )
        assert resp.status_code == 200, resp.text

        rows = _reviews(session, order["id"])
        assert len(rows) == 1, "the safety net duplicated the creation-time row"
        assert rows[0].id == original.id
        assert rows[0].reviewer_user_id == lab["reviewer"].id

    def test_creating_two_orders_assigns_each_exactly_once(
        self, client, session, lab
    ):
        _configure_default(client, lab, lab["reviewer"].id)

        first = _create_order(client, lab)
        second = _create_order(client, lab)

        assert len(_reviews(session, first["id"])) == 1
        assert len(_reviews(session, second["id"])) == 1

    def test_the_helper_is_a_no_op_when_the_order_already_has_a_reviewer(
        self, session, lab
    ):
        """Called directly, twice, the way a retry would."""
        from app.services.report_default_reviewer import (
            ensure_default_reviewer_assignment,
        )
        from app.models.laboratory import Order

        lab["tenant"].default_reviewer_id = lab["reviewer"].id
        session.add(lab["tenant"])
        order = Order(
            tenant_id=lab["tenant"].id,
            branch_id=lab["branch"].id,
            order_code=f"ORD-{uuid.uuid4().hex[:6]}",
            study_type_id=lab["study_type"].id,
        )
        session.add(order)
        session.commit()

        first = ensure_default_reviewer_assignment(
            session, tenant_id=order.tenant_id, order_id=order.id
        )
        session.commit()
        second = ensure_default_reviewer_assignment(
            session, tenant_id=order.tenant_id, order_id=order.id
        )
        session.commit()

        assert first is not None
        assert second is None
        assert len(_reviews(session, order.id)) == 1


# ---------------------------------------------------------------------------
# Explicit assignment wins; an unusable default never blocks anything
# ---------------------------------------------------------------------------

class TestExplicitAssignmentWins:
    def test_an_explicit_assignment_is_never_replaced_or_augmented(
        self, client, session, lab
    ):
        _configure_default(client, lab, lab["reviewer"].id)
        order = _create_order(client, lab)
        explicit = create_user(
            session, lab["tenant"], email="explicit@t1.example", roles=("reviewer",)
        )

        resp = client.put(
            f"/api/v1/laboratory/orders/{order['id']}/reviewers",
            json={"reviewer_ids": [str(explicit.id)]},
            headers=auth_headers(lab["admin"]),
        )
        assert resp.status_code == 200, resp.text

        rows = _reviews(session, order["id"])
        assert [r.reviewer_user_id for r in rows] == [explicit.id]

        # And submitting later does not bring the default back.
        report = _draft_report(session, lab, order["id"])
        client.post(SUBMIT.format(report.id), json={}, headers=auth_headers(lab["author"]))
        rows = _reviews(session, order["id"])
        assert [r.reviewer_user_id for r in rows] == [explicit.id]


class TestAnUnusableDefaultNeverBlocksOrderCreation:
    def test_no_default_configured_creates_the_order_with_no_reviewer(
        self, client, session, lab
    ):
        order = _create_order(client, lab)

        assert _reviews(session, order["id"]) == []
        assert _reviewers_via_api(client, lab, order["id"]) == []

    def test_a_default_who_lost_the_reviewer_role_is_skipped(
        self, client, session, lab
    ):
        _configure_default(client, lab, lab["reviewer"].id)
        role = session.exec(select(Role).where(Role.code == "reviewer")).first()
        link = session.exec(
            select(UserRoleLink).where(
                UserRoleLink.user_id == lab["reviewer"].id,
                UserRoleLink.role_id == role.id,
            )
        ).first()
        session.delete(link)
        session.commit()

        order = _create_order(client, lab)

        assert _reviews(session, order["id"]) == []

    def test_a_deactivated_default_is_skipped(self, client, session, lab):
        _configure_default(client, lab, lab["reviewer"].id)
        reviewer = session.get(type(lab["reviewer"]), lab["reviewer"].id)
        reviewer.is_active = False
        session.add(reviewer)
        session.commit()

        order = _create_order(client, lab)

        assert _reviews(session, order["id"]) == []

    def test_a_cross_tenant_default_is_skipped(self, client, session, lab):
        """The configuration endpoint refuses this, so it is reachable only by
        a direct write — checked defensively because the consequence would be
        one tenant's user assigned to another's order."""
        other_tenant = create_tenant(session, name="Another Laboratory")
        create_branch(session, other_tenant, code="OTHER")
        foreign = create_user(
            session, other_tenant, email="rev@t2.example", roles=("reviewer",)
        )
        lab["tenant"].default_reviewer_id = foreign.id
        session.add(lab["tenant"])
        session.commit()

        order = _create_order(client, lab)

        assert _reviews(session, order["id"]) == []

    def test_a_dangling_default_pointer_cannot_exist_in_the_first_place(
        self, session, lab
    ):
        """`fk_tenant_default_reviewer_id_app_user` (v1_3_1 §3a) is
        `ON DELETE SET NULL`, so the pointer cannot outlive the user it names.
        The resolver still checks `candidate is None` defensively, but the
        database makes that branch unreachable — asserted here so the guard is
        documented as belt-and-braces rather than mistaken for a live case."""
        from sqlalchemy.exc import IntegrityError

        lab["tenant"].default_reviewer_id = uuid.uuid4()
        session.add(lab["tenant"])
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


# ---------------------------------------------------------------------------
# The safety net still earns its place
# ---------------------------------------------------------------------------

class TestTheSubmitSafetyNet:
    def test_a_default_configured_AFTER_the_order_still_materializes_at_submit(
        self, client, session, lab
    ):
        """The case the safety net exists for, alongside orders created before
        this release and orders created while no default was configured."""
        order = _create_order(client, lab)
        assert _reviews(session, order["id"]) == []

        _configure_default(client, lab, lab["reviewer"].id)
        report = _draft_report(session, lab, order["id"])

        resp = client.post(
            SUBMIT.format(report.id), json={}, headers=auth_headers(lab["author"])
        )
        assert resp.status_code == 200, resp.text

        rows = _reviews(session, order["id"])
        assert len(rows) == 1
        assert rows[0].reviewer_user_id == lab["reviewer"].id
        assert rows[0].status == ReviewStatus.PENDING

    def test_submission_still_fails_closed_with_no_usable_default(
        self, client, session, lab
    ):
        order = _create_order(client, lab)
        report = _draft_report(session, lab, order["id"])

        resp = client.post(
            SUBMIT.format(report.id), json={}, headers=auth_headers(lab["author"])
        )
        assert resp.status_code == 400, resp.text
        assert "without reviewers assigned" in resp.text


# ---------------------------------------------------------------------------
# No authority is conferred
# ---------------------------------------------------------------------------

class TestNoAuthorityIsConferred:
    def test_the_assignment_grants_no_role_or_permission(
        self, client, session, lab
    ):
        from app.core.rbac import get_user_permissions, get_user_roles

        before = (
            set(get_user_roles(lab["reviewer"].id, session)),
            set(get_user_permissions(lab["reviewer"].id, session)),
        )
        _configure_default(client, lab, lab["reviewer"].id)
        _create_order(client, lab)
        session.expire_all()
        after = (
            set(get_user_roles(lab["reviewer"].id, session)),
            set(get_user_permissions(lab["reviewer"].id, session)),
        )
        assert before == after

    def test_it_does_not_let_the_reviewer_approve_a_draft(
        self, client, session, lab
    ):
        """Assignment satisfies one half of Block A's double lock and nothing
        else. Approval keeps its own lifecycle guard, which refuses DRAFT."""
        _configure_default(client, lab, lab["reviewer"].id)
        order = _create_order(client, lab)
        report = _draft_report(session, lab, order["id"])

        resp = client.post(
            f"/api/v1/reports/{report.id}/approve",
            json={},
            headers=auth_headers(lab["reviewer"]),
        )
        assert resp.status_code == 400, resp.text
        session.expire_all()
        assert session.get(type(report), report.id).status == ReportStatus.DRAFT


def _draft_report(session: Session, lab, order_id):
    """A DRAFT report on an existing order, built directly — these tests are
    about the ORDER's reviewer assignment, not about report creation."""
    from app.models.report import Report, ReportVersion

    report = Report(
        tenant_id=lab["tenant"].id,
        branch_id=lab["branch"].id,
        order_id=uuid.UUID(str(order_id)),
        status=ReportStatus.DRAFT,
        title="Clinical findings",
    )
    session.add(report)
    session.flush()
    version = ReportVersion(report_id=report.id, version_no=1, is_current=True)
    session.add(version)
    session.commit()
    session.refresh(report)
    session.refresh(version)
    # The presentation route rewrites the current version's stored JSON, so a
    # version with no body is refused (409) before authorization is relevant.
    _store_body(
        session,
        version,
        lab["tenant"],
        {
            "base": {},
            "sections": [],
            "signatureMetadata": {
                "show_signature_section": False,
                "require_digital_signature": False,
            },
        },
    )
    return report
