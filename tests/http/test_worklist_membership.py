"""Céluma 1.5 (#85) — what "Pendientes" means and one consistent worklist snapshot.

Contract: celuma-engineering/specs/lists/search-and-pagination.md ("Mi Lista de Trabajo").
Synthetic data only. Each row carries three separate facts — the record's state
(`entity_status`), the user's own review decision (`review_decision`) and the step that
falls to the user now (`next_step`) — and, for an assignment the user cannot advance,
whose step is next (`waiting_on`). The review/sign steps are the existing approval and
signing contracts (`report_authorization`), resolved once per request; nothing here
changes who may approve or sign.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, text
from sqlmodel import select

from app.models.assignment import Assignment
from app.models.enums import ReviewStatus
from app.models.report import Report
from app.models.report_review import ReportReview
from app.models.permission import Permission
from app.models.role import Role
from app.models.role_permission import RolePermission
from app.services import report_authorization as authz

from .factories import auth_headers, create_user
from .test_list_search_and_pagination import Lab

API = "/api/v1"
T0 = datetime(2026, 11, 20, 12, 0, 0)


def worklist(client, user, **params):
    response = client.get(f"{API}/me/worklist", params=params, headers=auth_headers(user))
    assert response.status_code == 200, response.text
    return response.json()


def rows(client, user):
    def key(i):
        return i["kind"], (i["display_id"] if i["item_type"] == "sample" else i["order_code"] or i["display_id"])
    return {key(i): i for i in worklist(client, user, scope="all")["items"]}


def review_row(lab, order, report, user, status=ReviewStatus.PENDING, at=None):
    return lab.add(ReportReview(tenant_id=lab.tenant.id, order_id=order.id, report_id=report.id,
                                reviewer_user_id=user.id, status=status, assigned_at=at or T0))


def assign(lab, item_type, item_id, user, at=None):
    return lab.add(Assignment(tenant_id=lab.tenant.id, item_type=item_type, item_id=item_id,
                              assignee_user_id=user.id, assigned_at=at or T0))


def decide(client, review, user, decision):
    body = {"decision": decision, **({"comment": "Corregir"} if decision == "rejected" else {})}
    return client.post(f"{API}/report-reviews/{review.id}/decision", json=body, headers=auth_headers(user))


@pytest.fixture
def lab(session):
    return Lab(session, "Membresia")


def users(session, lab):
    t = lab.tenant
    return {
        "reviewer": create_user(session, t, email="rev@m.invalid", roles=("reviewer",)),
        "co_reviewer": create_user(session, t, email="corev@m.invalid", roles=("reviewer",)),
        "author": create_user(session, t, email="autor@m.invalid", roles=("pathologist",)),
        "author_reviewer": create_user(session, t, email="ambos@m.invalid", roles=("pathologist", "reviewer")),
        "tech": create_user(session, t, email="tec@m.invalid", roles=("lab_tech",)),
    }


class TestReviewRows:
    def test_review_decision_entity_status_and_next_step_are_separate(self, client, session, lab):
        u = users(session, lab)
        p = lab.patient("Ana", "Ejemplo", "PAC-EJ-1")
        o_rev = lab.order("ORD-REV", patient=p)
        r_rev = lab.report(o_rev, "R", status="IN_REVIEW")
        review_row(lab, o_rev, r_rev, u["reviewer"])

        # A co-reviewer rejects: the report goes back to DRAFT and my PENDING row has nothing to do.
        o_back = lab.order("ORD-DEVUELTA", patient=p)
        r_back = lab.report(o_back, "R", status="IN_REVIEW")
        review_row(lab, o_back, r_back, u["reviewer"])
        theirs = review_row(lab, o_back, r_back, u["co_reviewer"])
        assert decide(client, theirs, u["co_reviewer"], "rejected").status_code == 200

        # I rejected: my task is done.
        o_rej = lab.order("ORD-RECHACE", patient=p)
        r_rej = lab.report(o_rej, "R", status="IN_REVIEW")
        mine = review_row(lab, o_rej, r_rej, u["reviewer"])
        assert decide(client, mine, u["reviewer"], "rejected").status_code == 200

        o_pub = lab.order("ORD-PUBLICADA", patient=p)
        review_row(lab, o_pub, lab.report(o_pub, "R", status="PUBLISHED"), u["reviewer"])
        o_ret = lab.order("ORD-RETIRADA", patient=p)
        review_row(lab, o_ret, lab.report(o_ret, "R", status="RETRACTED"), u["reviewer"], status=ReviewStatus.APPROVED)

        got = {k[1]: (v["entity_status"], v["review_decision"], v["next_step"], v["actionable"])
               for k, v in rows(client, u["reviewer"]).items()}
        assert got == {
            "ORD-REV": ("IN_REVIEW", "PENDING", "review", True),
            "ORD-DEVUELTA": ("DRAFT", "PENDING", None, False),
            "ORD-RECHACE": ("DRAFT", "REJECTED", None, False),
            "ORD-PUBLICADA": ("PUBLISHED", "PENDING", None, False),
            "ORD-RETIRADA": ("RETRACTED", "APPROVED", None, False),
        }
        # The legacy field keeps its 1.4 meaning (the review decision) for older clients.
        assert rows(client, u["reviewer"])[("review", "ORD-RECHACE")]["item_status"] == "REJECTED"

    def test_por_firmar_for_the_approver_and_an_undecided_co_reviewer(self, client, session, lab):
        u = users(session, lab)
        o = lab.order("ORD-FIRMA")
        r = lab.report(o, "R", status="IN_REVIEW")
        mine = review_row(lab, o, r, u["reviewer"])
        review_row(lab, o, r, u["co_reviewer"])
        assert decide(client, mine, u["reviewer"], "approved").status_code == 200

        approver = rows(client, u["reviewer"])[("review", "ORD-FIRMA")]
        co = rows(client, u["co_reviewer"])[("review", "ORD-FIRMA")]
        assert (approver["entity_status"], approver["review_decision"], approver["next_step"]) == ("APPROVED", "APPROVED", "sign")
        assert (co["entity_status"], co["review_decision"], co["next_step"]) == ("APPROVED", "PENDING", "sign")

    def test_no_review_or_sign_step_without_the_role_capability_or_assignment(self, client, session, lab):
        u = users(session, lab)
        admin = lab.admin  # superuser: every capability, but not the reviewer role
        o_in, o_ok = lab.order("ORD-EN-REVISION"), lab.order("ORD-APROBADA")
        r_in, r_ok = lab.report(o_in, "R", status="IN_REVIEW"), lab.report(o_ok, "R", status="APPROVED")
        for user in (admin, u["author"]):  # assigned review rows, no reviewer role
            review_row(lab, o_in, r_in, user)
            review_row(lab, o_ok, r_ok, user)
        for user in (admin, u["author"]):
            steps = {k[1]: v["next_step"] for k, v in rows(client, user).items()}
            assert steps == {"ORD-EN-REVISION": None, "ORD-APROBADA": None}

        # Assignment: a reviewer who is not assigned to the order has no row at all.
        assert rows(client, u["co_reviewer"]) == {}

        # Capability: the reviewer role without `reports:sign` keeps "review" but loses "sign".
        review_row(lab, o_in, r_in, u["reviewer"])
        review_row(lab, o_ok, r_ok, u["reviewer"])
        sign = session.exec(select(Permission).where(Permission.code == "reports:sign")).one()
        role = session.exec(select(Role).where(Role.code == "reviewer")).one()
        link = session.exec(select(RolePermission).where(RolePermission.role_id == role.id,
                                                         RolePermission.permission_id == sign.id)).one()
        session.delete(link)
        session.commit()
        steps = {k[1]: v["next_step"] for k, v in rows(client, u["reviewer"]).items()}
        assert steps == {"ORD-EN-REVISION": "review", "ORD-APROBADA": None}

    def test_other_tenants_rows_never_appear(self, client, session, lab):
        u = users(session, lab)
        other = Lab(session, "OtroLab")
        foreign_user = create_user(session, other.tenant, email="rev@otro.invalid", roles=("reviewer",))
        fo = other.order("ORD-AJENA")
        fr = other.report(fo, "R", status="APPROVED")
        review_row(other, fo, fr, foreign_user)
        assign(other, "lab_order", fo.id, foreign_user)
        # The same user id never reaches another tenant's rows, and rows here stay here.
        o = lab.order("ORD-PROPIA")
        review_row(lab, o, lab.report(o, "R", status="APPROVED"), u["reviewer"])
        assert set(k[1] for k in rows(client, u["reviewer"])) == {"ORD-PROPIA"}
        assert set(k[1] for k in rows(client, foreign_user)) == {"ORD-AJENA"}

    def test_steps_match_the_approval_and_signing_contracts(self, client, session, lab):
        """Parity: next_step == "review" iff authorize_approval passes, "sign" iff authorize_signing passes."""
        u = users(session, lab)
        people = [u["reviewer"], u["author"], u["author_reviewer"], u["tech"], lab.admin]
        cases = 0
        for status in ("DRAFT", "IN_REVIEW", "APPROVED", "PUBLISHED", "RETRACTED"):
            for decision in (ReviewStatus.PENDING, ReviewStatus.APPROVED, ReviewStatus.REJECTED):
                o = lab.order(f"P-{status}-{decision.value}")
                r = lab.report(o, "R", status=status)
                for person in people:
                    review_row(lab, o, r, person, status=decision)
        for person in people:
            for (kind, code), row in rows(client, person).items():
                report = session.get(Report, row["item_id"])
                expected = None
                for step, contract in (("review", authz.authorize_approval), ("sign", authz.authorize_signing)):
                    try:
                        contract(session, report, person)
                        expected = step
                    except authz.ReportAuthorizationError:
                        pass
                assert row["next_step"] == expected, (person.email, code)
                cases += 1
        assert cases == 5 * 3 * len(people)


class TestAssignmentRows:
    def test_report_assignment_waits_and_comes_back_on_draft(self, client, session, lab):
        u = users(session, lab)
        author, reviewer = u["author"], u["reviewer"]
        o = lab.order("ORD-AUTOR")
        r = lab.report(o, "R", status="DRAFT")
        assign(lab, "report", r.id, author)
        review = review_row(lab, o, r, reviewer)

        def mine():
            row = rows(client, author)[("assignment", "ORD-AUTOR")]
            return row["entity_status"], row["next_step"], row["waiting_on"], row["actionable"]

        assert mine() == ("DRAFT", "work", None, True)
        r.status = "IN_REVIEW"
        lab.add(r)
        assert mine() == ("IN_REVIEW", None, "review", False)
        assert decide(client, review, reviewer, "rejected").status_code == 200
        assert mine() == ("DRAFT", "work", None, True)  # back to Pendientes: the author can edit
        r.status = "APPROVED"
        lab.add(r)
        assert mine() == ("APPROVED", None, "signature", False)
        r.status = "PUBLISHED"
        lab.add(r)
        assert mine() == ("PUBLISHED", None, None, False)

    def test_a_draft_assignee_without_reports_edit_has_no_step(self, client, session, lab):
        u = users(session, lab)
        o = lab.order("ORD-TEC")
        assign(lab, "report", lab.report(o, "R", status="DRAFT").id, u["tech"])
        row = rows(client, u["tech"])[("assignment", "ORD-TEC")]
        assert (row["next_step"], row["waiting_on"], row["actionable"]) == (None, None, False)

    def test_an_assignee_who_is_also_the_reviewer_has_the_step_not_a_wait(self, client, session, lab):
        u = users(session, lab)
        both = u["author_reviewer"]
        o_in, o_ok = lab.order("ORD-AMBOS-REV"), lab.order("ORD-AMBOS-FIRMA")
        r_in, r_ok = lab.report(o_in, "R", status="IN_REVIEW"), lab.report(o_ok, "R", status="APPROVED")
        for o, r in ((o_in, r_in), (o_ok, r_ok)):
            assign(lab, "report", r.id, both)
            review_row(lab, o, r, both)
        got = {k: (v["next_step"], v["waiting_on"]) for k, v in rows(client, both).items()}
        assert got[("assignment", "ORD-AMBOS-REV")] == ("review", None)
        assert got[("assignment", "ORD-AMBOS-FIRMA")] == ("sign", None)

    def test_order_and_sample_assignments(self, client, session, lab):
        u = users(session, lab)
        tech = u["tech"]
        o_rev, o_sig = lab.order("ORD-EN-REV", status="REVIEW"), lab.order("ORD-EN-FIRMA", status="REVIEW")
        lab.report(o_rev, "R", status="IN_REVIEW")
        lab.report(o_sig, "R", status="APPROVED")
        for code, status in (("ORD-RECIBIDA", "RECEIVED"), ("ORD-DIAG", "DIAGNOSIS"), ("ORD-LIBERADA", "RELEASED"),
                             ("ORD-CERRADA", "CLOSED"), ("ORD-CANCELADA", "CANCELLED")):
            assign(lab, "lab_order", lab.order(code, status=status).id, tech)
        for o in (o_rev, o_sig):
            assign(lab, "lab_order", o.id, tech)
        base = lab.order("ORD-MUESTRAS")
        for code, state in (("M-PROC", "PROCESSING"), ("M-LISTA", "READY"), ("M-DANADA", "DAMAGED"), ("M-CANC", "CANCELLED")):
            assign(lab, "sample", lab.sample(base, code, state=state).id, tech)

        got = {(v["display_id"]): (v["entity_status"], v["next_step"], v["waiting_on"]) for v in rows(client, tech).values()}
        assert got == {
            "ORD-RECIBIDA": ("RECEIVED", "work", None),
            "ORD-DIAG": ("DIAGNOSIS", "work", None),
            "ORD-EN-REV": ("REVIEW", None, "review"),
            "ORD-EN-FIRMA": ("REVIEW", None, "signature"),
            "ORD-LIBERADA": ("RELEASED", None, None),
            "ORD-CERRADA": ("CLOSED", None, None),
            "ORD-CANCELADA": ("CANCELLED", None, None),
            "M-PROC": ("PROCESSING", "work", None),
            "M-LISTA": ("READY", None, None),
            "M-DANADA": ("DAMAGED", None, None),
            "M-CANC": ("CANCELLED", None, None),
        }

    def test_unassigned_and_removed_reviewer_rows_leave_todas(self, client, session, lab):
        u = users(session, lab)
        o = lab.order("ORD-SALE")
        a = assign(lab, "lab_order", o.id, u["tech"])
        assert ("assignment", "ORD-SALE") in rows(client, u["tech"])
        assert client.delete(f"{API}/assignments/{a.id}", headers=lab.headers).status_code == 200
        assert ("assignment", "ORD-SALE") not in rows(client, u["tech"])


def many_assignments(lab, n, user, *, same_time=False):
    out = []
    for i in range(n):
        o = lab.order(f"CU-{i + 1:04d}")
        at = T0 if same_time else T0 - timedelta(minutes=i)
        out.append(assign(lab, "lab_order", o.id, user, at=at))
    return out


class TestSnapshot:
    def test_scope_all_returns_every_row_in_one_response(self, client, session, lab):
        many_assignments(lab, 150, lab.admin)
        body = worklist(client, lab.admin, scope="all")
        assert (body["total"], len(body["items"]), body["page"], body["page_size"], body["has_more"], body["scope"]) == \
            (150, 150, 1, 150, False, "all")
        assert body["as_of"]
        codes = [i["display_id"] for i in body["items"]]
        assert codes == [f"CU-{i:04d}" for i in range(1, 151)]

    def test_paging_is_unchanged_for_legacy_callers(self, client, session, lab):
        many_assignments(lab, 25, lab.admin)
        body = worklist(client, lab.admin)
        assert (body["total"], len(body["items"]), body["page"], body["page_size"], body["has_more"]) == (25, 20, 1, 20, True)
        assert body.get("scope") is None and body.get("as_of") is None
        page2 = worklist(client, lab.admin, page=2, page_size=20)
        assert len(page2["items"]) == 5 and page2["has_more"] is False

    @pytest.mark.parametrize("params", [{"page": 1}, {"page_size": 100}, {"page": 2, "page_size": 10}])
    def test_scope_all_cannot_be_paged(self, client, lab, params):
        r = client.get(f"{API}/me/worklist", params={"scope": "all", **params}, headers=lab.headers)
        assert r.status_code == 422

    def test_identical_timestamps_order_by_id_and_page_walks_are_complete(self, client, session, lab):
        made = many_assignments(lab, 130, lab.admin, same_time=True)
        expected = [str(a.id) for a in sorted(made, key=lambda a: str(a.id), reverse=True)]
        assert [i["id"] for i in worklist(client, lab.admin, scope="all")["items"]] == expected
        # A status UPDATE between page requests no longer reorders ties (the id decides).
        first = worklist(client, lab.admin, page=1, page_size=100)
        session.execute(text("UPDATE assignment SET assigned_by_user_id = :u WHERE id = :id"),
                        {"u": lab.admin.id, "id": first["items"][-1]["id"]})
        session.commit()
        second = worklist(client, lab.admin, page=2, page_size=100)
        walked = [i["id"] for i in first["items"] + second["items"]]
        assert walked == expected and len(set(walked)) == 130

    def test_the_snapshot_begins_before_any_worklist_read_and_is_transaction_scoped(self, client, session, engine, lab):
        many_assignments(lab, 3, lab.admin)
        statements, isolation = [], []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(" ".join(statement.split())[:80])
            if statement.startswith("SELECT now()"):
                probe = cursor.connection.cursor()
                probe.execute("SHOW transaction_isolation")
                isolation.append(probe.fetchone()[0])
                probe.execute("SHOW transaction_read_only")
                isolation.append(probe.fetchone()[0])

        event.listen(engine, "after_cursor_execute", record)
        try:
            worklist(client, lab.admin, scope="all")
        finally:
            event.remove(engine, "after_cursor_execute", record)
        set_at = next(i for i, s in enumerate(statements) if s.startswith("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        reads = [i for i, s in enumerate(statements) if "FROM assignment" in s or "FROM report_review" in s]
        assert reads and min(reads) > set_at
        assert isolation == ["repeatable read", "on"]
        # Transaction-scoped: the endpoint ended it; the session's next transaction and the pool are at the default.
        assert session.execute(text("SHOW transaction_isolation")).scalar_one() == "read committed"
        with engine.connect() as conn:
            assert conn.execute(text("SHOW transaction_isolation")).scalar_one() == "read committed"
        # The paged mode does not use the snapshot.
        statements.clear()
        event.listen(engine, "after_cursor_execute", record)
        try:
            worklist(client, lab.admin, page=1, page_size=20)
        finally:
            event.remove(engine, "after_cursor_execute", record)
        assert not any(s.startswith("SET TRANSACTION") for s in statements)

    def test_same_total_churn_during_the_request_is_not_mixed_in(self, client, session, engine, lab):
        """A commit from another connection lands after the assignments are read and before the
        reviews are: the response is the state before it, entirely, and the next request is after it."""
        rows_ = many_assignments(lab, 120, lab.admin)
        rev = create_user(session, lab.tenant, email="rev@churn.invalid", roles=("reviewer",))
        o = lab.order("ORD-REV-CHURN")
        r = lab.report(o, "R", status="IN_REVIEW")
        review = review_row(lab, o, r, lab.admin)
        assign(lab, "lab_order", o.id, rev)
        fresh_order = lab.order("CU-NUEVA")
        # Plain values: touching an expired ORM object inside the listener would reload it
        # mid-request and mask what the endpoint's own statements see.
        ids = {"gone": rows_[9].id, "order": fresh_order.id, "review": review.id, "report": r.id,
               "tenant": lab.tenant.id, "user": lab.admin.id}
        fired = []

        def churn(conn, cursor, statement, parameters, context, executemany):
            if fired or "FROM assignment" not in statement:
                return
            fired.append(True)
            with engine.begin() as other:  # another connection, committed before the endpoint reads on
                other.execute(text("UPDATE assignment SET unassigned_at = now() WHERE id = :id"), {"id": ids["gone"]})
                other.execute(text("INSERT INTO assignment (id, tenant_id, item_type, item_id, assignee_user_id, assigned_at) "
                                   "VALUES (gen_random_uuid(), :t, 'lab_order', :o, :u, :at)"),
                              {"t": ids["tenant"], "o": ids["order"], "u": ids["user"], "at": T0 + timedelta(minutes=5)})
                other.execute(text("UPDATE report_review SET status = 'REJECTED', decision_at = now() WHERE id = :id"), {"id": ids["review"]})
                other.execute(text("UPDATE report SET status = 'DRAFT' WHERE id = :id"), {"id": ids["report"]})

        event.listen(engine, "after_cursor_execute", churn)
        try:
            body = worklist(client, lab.admin, scope="all")
        finally:
            event.remove(engine, "after_cursor_execute", churn)
        assert fired
        codes = [i["display_id"] for i in body["items"]]
        review_item = next(i for i in body["items"] if i["kind"] == "review")
        assert body["total"] == 121 and "CU-0010" in codes and "CU-NUEVA" not in codes
        assert (review_item["review_decision"], review_item["entity_status"]) == ("PENDING", "IN_REVIEW")
        session.expire_all()
        after = worklist(client, lab.admin, scope="all")
        codes_after = [i["display_id"] for i in after["items"]]
        review_after = next(i for i in after["items"] if i["kind"] == "review")
        assert after["total"] == 121 and "CU-0010" not in codes_after and "CU-NUEVA" in codes_after
        assert (review_after["review_decision"], review_after["entity_status"]) == ("REJECTED", "DRAFT")

    def test_statement_count_does_not_grow_with_rows(self, client, session, engine, lab):
        def count():
            n = []
            listener = lambda *a: n.append(1)  # noqa: E731
            event.listen(engine, "after_cursor_execute", listener)
            try:
                worklist(client, lab.admin, scope="all")
            finally:
                event.remove(engine, "after_cursor_execute", listener)
            return len(n)

        def grow(k):
            for i in range(k):
                o = lab.order(f"G-{lab._seq}-{i}")
                assign(lab, "lab_order", o.id, lab.admin)
                r = lab.report(lab.order(f"GR-{lab._seq}-{i}", status="REVIEW"), "R", status="IN_REVIEW")
                review_row(lab, session.get(type(o), r.order_id), r, lab.admin)
                assign(lab, "report", r.id, lab.admin)
                assign(lab, "sample", lab.sample(o, f"GS-{lab._seq}-{i}").id, lab.admin)

        grow(2)
        few = count()
        grow(10)
        assert count() == few


class TestBaseline:
    def test_synthetic_1000_row_baseline(self, client, session, lab):
        """Records size and latency; asserts only completeness (timings vary by machine)."""
        many_assignments(lab, 600, lab.admin)
        for i in range(400):
            o = lab.order(f"RV-{i:03d}")
            review_row(lab, o, lab.report(o, f"R {i}", status="PUBLISHED"), lab.admin,
                       status=ReviewStatus.APPROVED, at=T0 - timedelta(days=1, minutes=i))

        def timed(**params):
            best, body, size = None, None, 0
            for _ in range(5):
                t = time.perf_counter()
                r = client.get(f"{API}/me/worklist", params=params, headers=lab.headers)
                ms = (time.perf_counter() - t) * 1000
                best = ms if best is None else min(best, ms)
                body, size = r.json(), len(r.content)
            return round(best), body, size

        all_ms, all_body, all_size = timed(scope="all")
        page_ms, page_body, page_size = timed(page=1, page_size=100)
        walk_t = time.perf_counter()
        page, walked = 1, 0
        while True:
            b = client.get(f"{API}/me/worklist", params={"page": page, "page_size": 100}, headers=lab.headers).json()
            walked += len(b["items"])
            if not b["has_more"]:
                break
            page += 1
        walk_ms = round((time.perf_counter() - walk_t) * 1000)
        report = {"rows": all_body["total"], "scope_all_ms_best_of_5": all_ms, "scope_all_bytes": all_size,
                  "one_page_100_ms_best_of_5": page_ms, "one_page_100_bytes": page_size,
                  "ten_page_walk_ms": walk_ms, "walk_requests": page}
        print("\n#85 baseline " + json.dumps(report))
        assert all_body["total"] == len(all_body["items"]) == 1000 and walked == 1000
