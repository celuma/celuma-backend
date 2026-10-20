"""Céluma 1.4 — reviewer availability and recovery around amendments (synthetic data only).

A review row is an assignment, not availability: the signer of an old emission may have lost the
`reviewer` role since. The review workflow must (a) not be entered with nobody able to act,
(b) never be emptied of eligible reviewers while in progress, (c) stay repairable by a manager
with a qualified same-tenant reviewer — never by administrator self-promotion — and (d) keep the
delivered emission's review evidence when the assignment is replaced and the amendment cancelled.
"""
import pytest
from sqlmodel import select

from app.core.rbac import replace_user_roles
from app.models.enums import ReportStatus, ReviewStatus
from app.models.events import OrderEvent
from app.models.report import Report, ReportVersion
from app.models.report_review import ReportReview
from app.services import order_lifecycle

from concurrent.futures import ThreadPoolExecutor

from .numbering_support import new_client, run_parallel
from .test_report_amendments import parallel, _approve, _sign  # noqa: F401
from .test_report_write_serialization import Gate, _hold_render, _loaded
from .factories import auth_headers, create_tenant, create_user
from .test_report_amendments import (
    CONFIRM, _cancel, _events, _fingerprint, _open, _save, _submit, _url, lab,  # noqa: F401
)


def _put_reviewers(client, lab, ids, actor=None):
    return client.put(
        f"/api/v1/laboratory/orders/{lab['order'].id}/reviewers",
        json={"reviewer_ids": [str(i) for i in ids]},
        headers=auth_headers(actor or lab["admin"]),
    )


def _stale_assignee(session, lab):
    """The old signer is still assigned but is now only an administrator."""
    replace_user_roles(lab["reviewer"].id, ["admin"], session)
    session.commit()


def _reviews(session, lab):
    session.expire_all()
    return session.exec(select(ReportReview).where(ReportReview.order_id == lab["order"].id)).all()


def _status(session, lab):
    session.expire_all()
    return session.get(Report, lab["report"].id).status


def _qualified(session, lab, email="qualified@synthetic.example"):
    return create_user(session, lab["tenant"], email=email, roles=("reviewer",))


class TestSubmitNeedsAnEligibleReviewer:
    def test_a_stale_sole_assignee_is_refused_with_actionable_spanish_and_no_side_effects(self, client, session, lab):
        _stale_assignee(session, lab)
        assert _open(client, lab, actor=lab["admin"]).status_code == 200
        assert _save(client, lab).status_code == 200
        events_before = len(session.exec(select(OrderEvent).where(OrderEvent.order_id == lab["order"].id)).all())

        response = _submit(client, lab)

        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "reviewers_unavailable"
        assert "revisor" in detail["message"] and "administrador no puede aprobar" in detail["message"]
        assert _status(session, lab) == ReportStatus.DRAFT
        assert [r.status for r in _reviews(session, lab)] == [ReviewStatus.PENDING]  # opening already reset it
        assert len(session.exec(select(OrderEvent).where(OrderEvent.order_id == lab["order"].id)).all()) == events_before

    def test_an_inactive_assignee_is_refused(self, client, session, lab):
        assert _open(client, lab).status_code == 200
        assert _save(client, lab).status_code == 200
        lab["reviewer"].is_active = False
        session.add(lab["reviewer"])
        session.commit()
        assert _submit(client, lab).status_code == 409

    def test_the_tenant_default_never_replaces_a_stale_explicit_assignment(self, client, session, lab):
        default = _qualified(session, lab, "default@synthetic.example")
        lab["tenant"].default_reviewer_id = default.id
        session.add(lab["tenant"])
        _stale_assignee(session, lab)
        assert _open(client, lab, actor=lab["admin"]).status_code == 200
        assert _save(client, lab).status_code == 200
        assert _submit(client, lab).status_code == 409
        assert [r.reviewer_user_id for r in _reviews(session, lab)] == [lab["reviewer"].id]

    def test_an_administrator_who_is_also_a_reviewer_is_eligible(self, client, session, lab):
        replace_user_roles(lab["reviewer"].id, ["admin", "reviewer"], session)
        session.commit()
        assert _open(client, lab, actor=lab["admin"]).status_code == 200
        assert _save(client, lab).status_code == 200
        assert _submit(client, lab).status_code == 200

    def test_recovery_assign_a_qualified_reviewer_then_submit_approve_and_sign(self, client, session, lab):
        original = _fingerprint(session, lab["v1"])
        replacement = _qualified(session, lab)
        _stale_assignee(session, lab)
        assert _open(client, lab, actor=lab["admin"]).status_code == 200
        assert _save(client, lab).status_code == 200
        assert _submit(client, lab).status_code == 409

        assigned = _put_reviewers(client, lab, [replacement.id])
        assert assigned.status_code == 200, assigned.text
        assert _submit(client, lab).status_code == 200

        admin_approval = client.post(_url(lab["report"], "/approve"), json={}, headers=auth_headers(lab["admin"]))
        assert admin_approval.status_code == 403
        approved = client.post(_url(lab["report"], "/approve"), json={}, headers=auth_headers(replacement))
        assert approved.status_code == 200, approved.text
        signed = client.post(_url(lab["report"], "/sign-and-publish"), json=CONFIRM, headers=auth_headers(replacement))
        assert signed.status_code == 200, signed.text
        assert _fingerprint(session, lab["v1"]) == original


class TestAssignmentRules:
    def test_unqualified_candidates_are_refused_without_side_effects(self, client, session, lab):
        foreign = create_user(session, create_tenant(session, name="Other"), email="x@other.example", roles=("reviewer",))
        admin_only = create_user(session, lab["tenant"], email="adm2@synthetic.example", roles=("admin",))
        inactive = _qualified(session, lab, "off@synthetic.example")
        inactive.is_active = False
        session.add(inactive)
        session.commit()
        before = [r.id for r in _reviews(session, lab)]
        assert _put_reviewers(client, lab, [foreign.id]).status_code == 400
        for candidate in (admin_only, inactive):
            response = _put_reviewers(client, lab, [candidate.id])
            assert response.status_code == 422, response.text
            assert "no puede asignarse" in response.json()["detail"]
        assert [r.id for r in _reviews(session, lab)] == before

    def test_an_administrator_cannot_self_assign_without_the_reviewer_role(self, client, session, lab):
        assert _put_reviewers(client, lab, [lab["admin"].id]).status_code == 422

    def test_clearing_is_still_allowed_for_a_draft_and_the_default_safety_net_is_untouched(self, client, session, lab):
        assert _open(client, lab).status_code == 200  # DRAFT amendment
        response = _put_reviewers(client, lab, [])
        assert response.status_code == 200, response.text
        assert _reviews(session, lab) == []

    def test_the_reviewer_list_says_who_can_no_longer_review(self, client, session, lab):
        _stale_assignee(session, lab)
        body = client.get(f"/api/v1/laboratory/orders/{lab['order'].id}/full", headers=auth_headers(lab["admin"])).json()
        reviewers = body["order"]["reviewers"]
        assert [r["eligible"] for r in reviewers] == [False]

    def test_assignment_takes_the_order_lifecycle_lock(self, client, session, lab, monkeypatch):
        calls = []
        real = order_lifecycle.lock_order
        monkeypatch.setattr(order_lifecycle, "lock_order", lambda s, oid: (calls.append(str(oid)), real(s, oid))[1])
        assert _put_reviewers(client, lab, [lab["reviewer"].id]).status_code == 200
        assert calls == [str(lab["order"].id)]


class TestInReviewContinuity:
    def _in_review(self, client, lab):
        assert _open(client, lab).status_code == 200
        assert _save(client, lab).status_code == 200
        assert _submit(client, lab).status_code == 200

    def test_the_last_reviewer_cannot_be_removed_while_in_review(self, client, session, lab):
        self._in_review(client, lab)
        before = len(session.exec(select(OrderEvent).where(OrderEvent.order_id == lab["order"].id)).all())
        response = _put_reviewers(client, lab, [])
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "review_requires_eligible_reviewer"
        assert [r.status for r in _reviews(session, lab)] == [ReviewStatus.PENDING]
        assert len(session.exec(select(OrderEvent).where(OrderEvent.order_id == lab["order"].id)).all()) == before

    def test_the_last_reviewer_cannot_be_removed_while_approved_awaiting_signature(self, client, session, lab):
        self._in_review(client, lab)
        assert client.post(_url(lab["report"], "/approve"), json={}, headers=auth_headers(lab["reviewer"])).status_code == 200
        assert _put_reviewers(client, lab, []).status_code == 409

    def test_replacement_is_allowed_and_cancel_restores_the_delivered_decision(self, client, session, lab):
        original_review_id = lab["review"].id
        replacement = _qualified(session, lab)
        opened = _open(client, lab)
        assert opened.status_code == 200
        amendment_id = opened.json()["amendment"]["id"] if "amendment" in opened.json() else None
        assert _save(client, lab).status_code == 200
        assert _submit(client, lab).status_code == 200

        replaced = _put_reviewers(client, lab, [replacement.id])
        assert replaced.status_code == 200, replaced.text
        assert [r.reviewer_user_id for r in _reviews(session, lab)] == [replacement.id]

        if amendment_id is None:
            from app.models.report_amendment import ReportAmendment
            amendment_id = str(session.exec(select(ReportAmendment)).first().id)
        original = _fingerprint(session, lab["v1"])
        cancelled = _cancel(client, lab, amendment_id, actor=lab["admin"])
        assert cancelled.status_code == 200, cancelled.text
        # The delivered emission's decision (APPROVED by the original reviewer) is back, same row id.
        restored = {r.id: r for r in _reviews(session, lab)}
        assert restored[original_review_id].status == ReviewStatus.APPROVED
        assert restored[original_review_id].reviewer_user_id == lab["reviewer"].id
        assert _status(session, lab) == ReportStatus.PUBLISHED
        assert _fingerprint(session, lab["v1"]) == original

    def test_replacing_during_review_lets_the_replacement_republish(self, client, session, lab):
        original = _fingerprint(session, lab["v1"])
        replacement = _qualified(session, lab)
        self._in_review(client, lab)
        assert _put_reviewers(client, lab, [replacement.id]).status_code == 200
        assert client.post(_url(lab["report"], "/approve"), json={}, headers=auth_headers(lab["reviewer"])).status_code == 403
        assert client.post(_url(lab["report"], "/approve"), json={}, headers=auth_headers(replacement)).status_code == 200
        assert client.post(_url(lab["report"], "/sign-and-publish"), json=CONFIRM, headers=auth_headers(replacement)).status_code == 200
        assert _fingerprint(session, lab["v1"]) == original


class TestConcurrency:
    def test_submit_and_clearing_the_reviewers_never_strand_a_report_in_review(self, parallel, client, session, lab):  # noqa: F811
        assert _open(client, lab).status_code == 200
        assert _save(client, lab).status_code == 200
        submit_url = _url(lab["report"], "/submit")
        put_url = f"/api/v1/laboratory/orders/{lab['order'].id}/reviewers"
        author, admin = auth_headers(lab["author"]), auth_headers(lab["admin"])
        results = run_parallel([
            lambda c: c.post(submit_url, json={}, headers=author),
            lambda c: c.put(put_url, json={"reviewer_ids": []}, headers=admin),
        ])
        codes = [getattr(r, "status_code", r) for r in results]
        session.expire_all()
        report_status = session.get(Report, lab["report"].id).status
        rows = session.exec(select(ReportReview).where(ReportReview.order_id == lab["order"].id)).all()
        # Whichever got the order lock first: either review was entered WITH its reviewer (and
        # clearing was refused), or the reviewers were cleared first and submission was refused.
        if report_status == ReportStatus.IN_REVIEW:
            assert codes == [200, 409] and len(rows) == 1
        else:
            assert report_status == ReportStatus.DRAFT and codes == [400, 200] and rows == []


class TestSigningClaim:
    """The order lock is released when a signature commits its publish claim, before the slow
    lock-free rendering: the fresh claim is what must keep the signer's assignment in place."""

    def _approved_amendment(self, client, lab):
        assert _open(client, lab).status_code == 200
        assert _save(client, lab).status_code == 200
        assert _submit(client, lab).status_code == 200
        assert _approve(client, lab).status_code == 200

    @pytest.mark.parametrize("route", ["/sign-and-publish", "/sign"])
    def test_the_signer_cannot_be_replaced_during_the_publish_claim(self, parallel, client, session, lab, monkeypatch, route):  # noqa: F811
        replacement = _qualified(session, lab)
        self._approved_amendment(client, lab)
        if route == "/sign":
            # The two-step route needs a generated PDF for the version before it signs.
            current = session.exec(select(ReportVersion).where(
                ReportVersion.report_id == lab["report"].id, ReportVersion.is_current == True)).one()  # noqa: E712
            generated = client.post(_url(lab["report"], f"/versions/{current.version_no}/generate-pdf"),
                                    json={}, headers=auth_headers(lab["author"]))
            assert generated.status_code == 200, generated.text
        _loaded(lab)
        order_id, report_id, replacement_id = str(lab["order"].id), lab["report"].id, str(replacement.id)
        rows_before = sorted(str(r.id) for r in _reviews(session, lab))
        events_before = len(session.exec(select(OrderEvent).where(OrderEvent.order_id == lab["order"].id)).all())
        gate = Gate()
        _hold_render(monkeypatch, gate)
        with ThreadPoolExecutor(max_workers=2) as pool:
            held = pool.submit(_sign, new_client(), lab, None, route)
            try:
                assert gate.reached.wait(15)
                refused = new_client().put(
                    f"/api/v1/laboratory/orders/{order_id}/reviewers",
                    json={"reviewer_ids": [replacement_id]}, headers=auth_headers(lab["admin"]),
                )
                # Rows and events are untouched while the signature is in flight.
                session.expire_all()
                assert sorted(str(r.id) for r in _reviews(session, lab)) == rows_before
            finally:
                gate.released.set()
            signed = held.result(timeout=60)
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["code"] == "report_publish_in_progress"
        assert signed.status_code == 200, signed.text
        session.expire_all()
        assert len(session.exec(select(OrderEvent).where(OrderEvent.order_id == lab["order"].id)).all()) >= events_before
        assert not [e for e in session.exec(select(OrderEvent).where(OrderEvent.order_id == lab["order"].id)).all()
                    if e.event_type.value == "REVIEWERS_REMOVED"]
        # Published under the signer who was assigned the whole time.
        report = session.get(Report, report_id)
        assert report.status == ReportStatus.PUBLISHED

    def test_assignment_committed_before_the_signature_wins_and_the_removed_signer_is_refused(self, client, session, lab):
        replacement = _qualified(session, lab)
        self._approved_amendment(client, lab)
        assert _put_reviewers(client, lab, [replacement.id]).status_code == 200
        refused = _sign(client, lab)  # lab["reviewer"] is no longer assigned
        assert refused.status_code in (403, 409), refused.text
        assert _status(session, lab) == ReportStatus.APPROVED

    def test_once_the_signature_has_finished_assignment_is_allowed_again(self, client, session, lab):
        self._approved_amendment(client, lab)
        assert _sign(client, lab).status_code == 200
        assert _status(session, lab) == ReportStatus.PUBLISHED
        assert _put_reviewers(client, lab, []).status_code == 200  # PUBLISHED clearing unchanged


class TestCancelReconcilesAReaddedReviewer:
    def test_removed_then_readded_original_ends_with_one_restored_row(self, client, session, lab):
        replacement = _qualified(session, lab)
        original_id, original_user = lab["review"].id, lab["reviewer"].id
        opened = _open(client, lab)
        assert opened.status_code == 200
        from app.models.report_amendment import ReportAmendment
        amendment_id = str(session.exec(select(ReportAmendment)).one().id)
        original = _fingerprint(session, lab["v1"])
        assert _put_reviewers(client, lab, [replacement.id]).status_code == 200
        assert _put_reviewers(client, lab, [original_user]).status_code == 200  # a NEW row for the same person
        assert _cancel(client, lab, amendment_id, actor=lab["admin"]).status_code == 200
        rows = _reviews(session, lab)
        mine = [r for r in rows if r.reviewer_user_id == original_user]
        assert [(r.id, r.status) for r in mine] == [(original_id, ReviewStatus.APPROVED)]
        assert len({r.reviewer_user_id for r in rows}) == len(rows)  # one row per reviewer
        assert _status(session, lab) == ReportStatus.PUBLISHED
        assert _fingerprint(session, lab["v1"]) == original

    def test_a_distinct_replacement_reviewer_is_kept_and_the_original_restored(self, client, session, lab):
        replacement = _qualified(session, lab)
        original_id, original_user = lab["review"].id, lab["reviewer"].id
        opened = _open(client, lab)
        assert opened.status_code == 200
        from app.models.report_amendment import ReportAmendment
        amendment_id = str(session.exec(select(ReportAmendment)).one().id)
        assert _put_reviewers(client, lab, [replacement.id]).status_code == 200
        assert _cancel(client, lab, amendment_id, actor=lab["admin"]).status_code == 200
        by_user = {r.reviewer_user_id: r for r in _reviews(session, lab)}
        assert set(by_user) == {original_user, replacement.id}
        assert by_user[original_user].id == original_id and by_user[original_user].status == ReviewStatus.APPROVED
        assert by_user[replacement.id].status == ReviewStatus.PENDING
