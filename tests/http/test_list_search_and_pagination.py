"""List search, filters, ordering and pagination (Céluma 1.4, issues #5/#6/#7).

Contract: `celuma-engineering/specs/lists/search-and-pagination.md`. These
tests drive the real endpoints against PostgreSQL (with `fuzzystrmatch` from
the 1.4 migration) using small synthetic tenants; the identifier cases mirror
`celuma-frontend/src/test/lib/search.test.ts` so both halves of the contract
are held to the same examples.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event

from app.models.assignment import Assignment
from app.models.enums import ReviewStatus
from app.models.laboratory import Label, Order, OrderLabel, Sample, SampleLabel
from app.models.patient import Patient
from app.models.report import Report, ReportVersion
from app.models.report_review import ReportReview
from app.models.requesting_physician import RequestingPhysician

from .factories import auth_headers, create_branch, create_tenant, create_user

SAMPLES = "/api/v1/laboratory/samples/"
ORDERS = "/api/v1/laboratory/orders/"
REPORTS = "/api/v1/reports/"
EPOCH = datetime(2026, 1, 5, 9, 0, 0)


class Lab:
    """A synthetic laboratory: tenant, branch, an admin and helpers to add rows."""

    def __init__(self, session, name: str):
        self.session = session
        self.tenant = create_tenant(session, name=name)
        self.branch = create_branch(session, self.tenant)
        self.admin = create_user(session, self.tenant, email=f"admin@{name.lower()}.invalid",
                                 full_name=f"Admin {name}")
        self.headers = auth_headers(self.admin)
        self._seq = 0

    def _tick(self) -> datetime:
        self._seq += 1
        return EPOCH + timedelta(minutes=self._seq)

    def add(self, obj):
        self.session.add(obj)
        self.session.commit()
        self.session.refresh(obj)
        return obj

    def patient(self, first: str, last: str, code: str) -> Patient:
        return self.add(Patient(tenant_id=self.tenant.id, branch_id=self.branch.id, patient_code=code,
                                first_name=first, last_name=last, created_at=self._tick()))

    def physician(self, first: str, last: str, code: str) -> RequestingPhysician:
        return self.add(RequestingPhysician(tenant_id=self.tenant.id, branch_id=self.branch.id,
                                            physician_code=code, first_name=first, last_name=last,
                                            full_name=f"{first} {last}"))

    def order(self, code: str, *, patient=None, physician=None, requested_by=None, status="RECEIVED",
              notes=None) -> Order:
        return self.add(Order(tenant_id=self.tenant.id, branch_id=self.branch.id, order_code=code,
                              patient_id=patient.id if patient else None,
                              requesting_physician_id=physician.id if physician else None,
                              requested_by=requested_by, status=status, notes=notes,
                              created_at=self._tick()))

    def sample(self, order: Order, code: str, *, type="BIOPSIA", state="RECEIVED") -> Sample:
        return self.add(Sample(tenant_id=self.tenant.id, branch_id=self.branch.id, order_id=order.id,
                               sample_code=code, type=type, state=state))

    def label(self, name: str) -> Label:
        return self.add(Label(tenant_id=self.tenant.id, name=name, color="#49B6AD"))

    def tag_sample(self, sample: Sample, label: Label) -> None:
        self.add(SampleLabel(sample_id=sample.id, label_id=label.id))

    def tag_order(self, order: Order, label: Label) -> None:
        self.add(OrderLabel(order_id=order.id, label_id=label.id))

    def assign(self, item_type: str, item_id, user) -> None:
        self.add(Assignment(tenant_id=self.tenant.id, item_type=item_type, item_id=item_id,
                            assignee_user_id=user.id))

    def report(self, order: Order, title: str, *, status="DRAFT", versions: int = 1) -> Report:
        report = self.add(Report(tenant_id=self.tenant.id, branch_id=self.branch.id, order_id=order.id,
                                 status=status, title=title, created_at=self._tick()))
        order.report_id = report.id
        self.add(order)
        for number in range(1, versions + 1):
            self.add(ReportVersion(report_id=report.id, version_no=number, is_current=number == versions))
        return report


def codes(response, key="samples", field="sample_code"):
    assert response.status_code == 200, response.text
    return [row[field] for row in response.json()[key]]


@pytest.fixture
def lab(session):
    """Tenant A with identifier neighbours, accented names and two pages of rows."""
    lab = Lab(session, "Alfa")
    munoz = lab.patient("Lorena", "Muñóz", "P-1")
    perez = lab.patient("Juan", "Pérez", "P-2")
    salas = lab.physician("Roberto", "Salas", "MS-1")
    first = lab.order("CU-1", patient=munoz, physician=salas)
    second = lab.order("BX-1", patient=perez, requested_by="Dra. Núñez")
    future = lab.order("CU-011026", patient=perez)
    lab.sample(first, "CU276262")
    lab.sample(first, "CU276263", state="PROCESSING")
    lab.sample(first, "CU276252", type="SANGRE")
    lab.sample(second, "BX276262", type="TEJIDO")
    lab.sample(second, "IHQ276262")
    lab.sample(second, "CU999999", state="READY")
    lab.sample(future, "CU-1001026")
    lab.munoz, lab.perez, lab.salas = munoz, perez, salas
    lab.first, lab.second, lab.future = first, second, future
    return lab


# ---------------------------------------------------------------------------
# Identifier vs human-text matching (#7)
# ---------------------------------------------------------------------------


class TestIdentifierMatching:
    def test_a_full_code_does_not_match_its_neighbours(self, client, lab):
        found = codes(client.get(SAMPLES, params={"page": 1, "q": "CU276262"}, headers=lab.headers))
        assert found == ["CU276262"]

    def test_a_digit_run_matches_across_prefixes(self, client, lab):
        found = codes(client.get(SAMPLES, params={"page": 1, "q": "276262"}, headers=lab.headers))
        assert sorted(found) == ["BX276262", "CU276262", "IHQ276262"]

    def test_digits_are_never_typo_corrected(self, client, lab):
        found = codes(client.get(SAMPLES, params={"page": 1, "q": "276264"}, headers=lab.headers))
        assert found == []

    @pytest.mark.parametrize("query", ["cu011026", "CU-011026", "cu 011026", "Cu.011026"])
    def test_case_and_separators_are_ignored(self, client, lab, query):
        # The order code CU-011026 holds exactly one sample.
        found = codes(client.get(SAMPLES, params={"page": 1, "q": query}, headers=lab.headers))
        assert "CU-1001026" in found

    def test_a_partial_code_is_a_literal_substring(self, client, lab):
        found = codes(client.get(SAMPLES, params={"page": 1, "q": "CU2762"}, headers=lab.headers))
        assert sorted(found) == ["CU276252", "CU276262", "CU276263"]

    def test_a_term_never_spans_two_fields(self, client, session, lab):
        # "CU2762" + an order code beginning "62" used to concatenate into
        # "cu276262…" on the client and match CU276262.
        spanning = lab.order("62-A", patient=lab.perez)
        lab.sample(spanning, "CU2762")
        found = codes(client.get(SAMPLES, params={"page": 1, "q": "CU276262"}, headers=lab.headers))
        assert found == ["CU276262"]


class TestHumanTextMatching:
    @pytest.mark.parametrize("query", ["munoz", "Muñóz", "MUNOZ", "muñoz"])
    def test_accents_and_case(self, client, lab, query):
        found = codes(client.get(SAMPLES, params={"page": 1, "q": query}, headers=lab.headers))
        assert sorted(found) == ["CU276252", "CU276262", "CU276263"]

    def test_a_conservative_typo_in_a_name(self, client, lab):
        found = codes(client.get(SAMPLES, params={"page": 1, "q": "munos"}, headers=lab.headers))
        assert sorted(found) == ["CU276252", "CU276262", "CU276263"]

    def test_a_typo_in_an_enum_word(self, client, lab):
        found = codes(client.get(SAMPLES, params={"page": 1, "q": "biosia"}, headers=lab.headers))
        assert set(found) == {"CU276262", "CU276263", "IHQ276262", "CU999999", "CU-1001026"}

    def test_short_terms_are_not_typo_corrected(self, client, lab):
        assert codes(client.get(SAMPLES, params={"page": 1, "q": "mun"}, headers=lab.headers))
        assert codes(client.get(SAMPLES, params={"page": 1, "q": "mnu"}, headers=lab.headers)) == []

    def test_terms_are_and_matched_across_fields(self, client, lab):
        found = codes(client.get(SAMPLES, params={"page": 1, "q": "perez 276262"}, headers=lab.headers))
        assert sorted(found) == ["BX276262", "IHQ276262"]

    def test_requester_and_physician_names(self, client, lab):
        found = codes(client.get(ORDERS, params={"page": 1, "q": "nunez"}, headers=lab.headers),
                      key="orders", field="order_code")
        assert found == ["BX-1"]
        found = codes(client.get(ORDERS, params={"page": 1, "q": "salas"}, headers=lab.headers),
                      key="orders", field="order_code")
        assert found == ["CU-1"]


# ---------------------------------------------------------------------------
# Pagination, ordering and totals (#6)
# ---------------------------------------------------------------------------


class TestPagination:
    def test_paged_response_carries_the_total(self, client, lab):
        body = client.get(SAMPLES, params={"page": 1, "page_size": 3}, headers=lab.headers).json()
        assert (body["total"], body["page"], body["page_size"], len(body["samples"])) == (7, 1, 3, 3)

    def test_walking_every_page_yields_each_row_once_in_order(self, client, lab):
        full = codes(client.get(SAMPLES, headers=lab.headers))
        walked = []
        for page in range(1, 4):
            walked += codes(client.get(SAMPLES, params={"page": page, "page_size": 3}, headers=lab.headers))
        assert walked == full
        assert len(set(walked)) == len(walked) == 7

    def test_ties_on_the_sort_key_still_page_without_duplicates(self, client, session, lab):
        # Every sample of one order shares the order-code sort key; the row id
        # breaks the tie, so a page boundary inside the tie loses nothing.
        for n in range(9):
            lab.sample(lab.first, f"TIE{n}")
        walked = []
        for page in range(1, 6):
            walked += codes(client.get(SAMPLES, params={"page": page, "page_size": 4, "sort": "order_code"},
                                       headers=lab.headers))
        assert len(walked) == len(set(walked)) == 16

    def test_a_page_past_the_end_is_empty_with_the_true_total(self, client, lab):
        body = client.get(SAMPLES, params={"page": 9, "page_size": 3}, headers=lab.headers).json()
        assert body["samples"] == [] and body["total"] == 7

    def test_search_filters_and_total_apply_before_paging(self, client, lab):
        body = client.get(SAMPLES, params={"page": 2, "page_size": 1, "q": "276262"}, headers=lab.headers).json()
        assert body["total"] == 3 and len(body["samples"]) == 1

    def test_sort_descending_by_code(self, client, lab):
        found = codes(client.get(SAMPLES, params={"page": 1, "page_size": 3, "sort": "sample_code", "order": "desc"},
                                 headers=lab.headers))
        assert found == ["IHQ276262", "CU999999", "CU276263"]

    def test_sort_by_patient_is_accent_insensitive(self, client, lab):
        found = client.get(SAMPLES, params={"page": 1, "sort": "patient"}, headers=lab.headers).json()["samples"]
        names = [row["order"]["patient"]["full_name"] for row in found]
        assert names == sorted(names, key=lambda n: n.replace("é", "e").lower())

    def test_page_size_is_bounded(self, client, lab):
        assert client.get(SAMPLES, params={"page_size": 101}, headers=lab.headers).status_code == 422
        assert client.get(SAMPLES, params={"page": 0}, headers=lab.headers).status_code == 422

    @pytest.mark.parametrize("params", [{"state": "BOGUS"}, {"type": "NOPE"}, {"label_id": "x"},
                                        {"sort": "tenant_id"}, {"order": "sideways"}])
    def test_invalid_parameters_are_rejected_not_ignored(self, client, lab, params):
        assert client.get(SAMPLES, params={"page": 1, **params}, headers=lab.headers).status_code == 422


class TestFilters:
    def test_filters_combine_with_and_across_and_or_within(self, client, lab):
        found = codes(client.get(SAMPLES, params=[("page", 1), ("state", "RECEIVED"), ("state", "READY"),
                                                  ("type", "BIOPSIA")], headers=lab.headers))
        assert sorted(found) == ["CU-1001026", "CU276262", "CU999999", "IHQ276262"]

    def test_comma_separated_values_are_accepted(self, client, lab):
        found = codes(client.get(SAMPLES, params={"page": 1, "state": "PROCESSING,READY"}, headers=lab.headers))
        assert sorted(found) == ["CU276263", "CU999999"]

    def test_label_assignee_patient_and_order_filters(self, client, lab):
        urgent = lab.label("Urgente")
        tagged = lab.sample(lab.second, "LBL-1")
        lab.tag_sample(tagged, urgent)
        lab.assign("sample", tagged.id, lab.admin)
        assert codes(client.get(SAMPLES, params={"page": 1, "label_id": str(urgent.id)}, headers=lab.headers)) == ["LBL-1"]
        assert codes(client.get(SAMPLES, params={"page": 1, "assignee_id": str(lab.admin.id)},
                                headers=lab.headers)) == ["LBL-1"]
        assert sorted(codes(client.get(SAMPLES, params={"page": 1, "patient_id": str(lab.munoz.id)},
                                       headers=lab.headers))) == ["CU276252", "CU276262", "CU276263"]
        assert codes(client.get(SAMPLES, params={"page": 1, "order_id": str(lab.future.id)},
                                headers=lab.headers)) == ["CU-1001026"]

    def test_label_and_assignee_names_are_searchable(self, client, lab):
        tagged = lab.sample(lab.second, "LBL-2")
        lab.tag_sample(tagged, lab.label("Segunda opinión"))
        assert codes(client.get(SAMPLES, params={"page": 1, "q": "opinion"}, headers=lab.headers)) == ["LBL-2"]

    def test_order_boolean_filters(self, client, lab):
        lab.report(lab.first, "Reporte CU-1")
        with_report = codes(client.get(ORDERS, params={"page": 1, "has_report": "true"}, headers=lab.headers),
                            key="orders", field="order_code")
        without = codes(client.get(ORDERS, params={"page": 1, "has_report": "false"}, headers=lab.headers),
                        key="orders", field="order_code")
        assert with_report == ["CU-1"] and sorted(without) == ["BX-1", "CU-011026"]


class TestFilterOptions:
    def test_options_cover_the_whole_collection_not_one_page(self, client, lab):
        late = lab.label("Zeta")
        tagged = lab.sample(lab.future, "ZZZ-LAST")
        lab.tag_sample(tagged, late)
        # The tagged sample is on the last page of a 1-row page size...
        body = client.get(SAMPLES, params={"page": 1, "page_size": 1}, headers=lab.headers).json()
        assert "ZZZ-LAST" not in [row["sample_code"] for row in body["samples"]]
        # ...and its label is still a filter choice.
        options = client.get(f"{SAMPLES}filter-options", headers=lab.headers).json()
        assert [o["text"] for o in options["labels"]] == ["Zeta"]
        assert {o["text"] for o in options["orders"]} == {"CU-1", "BX-1", "CU-011026"}
        assert {o["text"] for o in options["patients"]} == {"Lorena Muñóz", "Juan Pérez"}
        assert options["states"] == ["RECEIVED", "PROCESSING", "READY"]

    def test_order_options_report_requesters(self, client, lab):
        options = client.get(f"{ORDERS}filter-options", headers=lab.headers).json()
        assert options["has_requester"] is True
        assert [o["text"] for o in options["requesting_physicians"]] == ["Roberto Salas"]


# ---------------------------------------------------------------------------
# Tenant isolation and permissions
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    def test_another_tenants_rows_and_catalogs_never_appear(self, client, session, lab):
        other = Lab(session, "Beta")
        their_patient = other.patient("Lorena", "Muñóz", "P-1")
        their_order = other.order("CU-1", patient=their_patient)
        their_sample = other.sample(their_order, "CU276262")
        their_label = other.label("Ajena")
        other.tag_sample(their_sample, their_label)

        mine = client.get(SAMPLES, params={"page": 1, "q": "CU276262"}, headers=lab.headers).json()
        assert str(their_sample.id) not in [row["id"] for row in mine["samples"]]
        assert mine["total"] == 1
        everything = client.get(SAMPLES, headers=lab.headers).json()["samples"]
        assert {row["tenant_id"] for row in everything} == {str(lab.tenant.id)}
        # Another tenant's ids as filters simply match nothing.
        for params in ({"label_id": str(their_label.id)}, {"patient_id": str(their_patient.id)},
                       {"order_id": str(their_order.id)}):
            body = client.get(SAMPLES, params={"page": 1, **params}, headers=lab.headers).json()
            assert body["total"] == 0
        options = client.get(f"{SAMPLES}filter-options", headers=lab.headers).json()
        assert str(their_label.id) not in {o["value"] for o in options["labels"]}
        assert str(their_patient.id) not in {o["value"] for o in options["patients"]}

    @pytest.mark.parametrize("path", [SAMPLES, ORDERS, REPORTS, f"{SAMPLES}filter-options",
                                      f"{ORDERS}filter-options", f"{REPORTS}filter-options"])
    def test_lists_and_catalogs_require_read_permission(self, client, session, lab, path):
        outsider = create_user(session, lab.tenant, email="md@alfa.invalid", roles=("physician",))
        assert client.get(path, params={"page": 1} if not path.endswith("options") else None,
                          headers=auth_headers(outsider)).status_code == 403


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


class TestReportsList:
    def test_search_filter_and_enrichment(self, client, session, lab):
        reviewer = create_user(session, lab.tenant, email="rev@alfa.invalid", full_name="Rocío Ávila",
                               roles=("reviewer",))
        report = lab.report(lab.first, "Biopsia de próstata", status="IN_REVIEW", versions=2)
        lab.report(lab.second, "Citología", status="DRAFT")
        lab.add(ReportReview(tenant_id=lab.tenant.id, order_id=lab.first.id, report_id=report.id,
                             reviewer_user_id=reviewer.id, status=ReviewStatus.PENDING))

        body = client.get(REPORTS, params={"page": 1, "q": "avila"}, headers=lab.headers).json()
        assert body["total"] == 1
        row = body["reports"][0]
        assert row["version_no"] == 2 and row["order"]["order_code"] == "CU-1"
        assert [(r["name"], r["status"]) for r in row["reviewers"]] == [("Rocío Ávila", "pending")]

        by_status = client.get(REPORTS, params={"page": 1, "status": "DRAFT"}, headers=lab.headers).json()
        assert [r["title"] for r in by_status["reports"]] == ["Citología"]
        by_reviewer = client.get(REPORTS, params={"page": 1, "reviewer_id": str(reviewer.id)},
                                 headers=lab.headers).json()
        assert [r["title"] for r in by_reviewer["reports"]] == ["Biopsia de próstata"]
        options = client.get(f"{REPORTS}filter-options", headers=lab.headers).json()
        assert options["statuses"] == ["DRAFT", "IN_REVIEW"]
        assert [o["text"] for o in options["reviewers"]] == ["Rocío Ávila"]


# ---------------------------------------------------------------------------
# Compatibility and query count (#5: no per-row queries)
# ---------------------------------------------------------------------------


class TestCompatibilityAndQueryCount:
    def test_a_request_without_paging_returns_the_whole_collection(self, client, lab):
        body = client.get(SAMPLES, headers=lab.headers).json()
        assert len(body["samples"]) == body["total"] == 7
        assert body["page"] is None and body["page_size"] is None
        row = next(r for r in body["samples"] if r["sample_code"] == "CU276262")
        assert row["order"]["order_code"] == "CU-1"
        assert row["order"]["patient"]["full_name"] == "Lorena Muñóz"

    def test_order_items_keep_their_pre_1_4_shape(self, client, lab):
        lab.tag_order(lab.first, lab.label("Urgente"))
        lab.assign("lab_order", lab.first.id, lab.admin)
        row = next(r for r in client.get(ORDERS, headers=lab.headers).json()["orders"] if r["order_code"] == "CU-1")
        assert row["sample_count"] == 3 and row["has_report"] is False
        assert row["requesting_physician"]["full_name"] == "Roberto Salas"
        assert [label["name"] for label in row["labels"]] == ["Urgente"]
        assert [user["email"] for user in row["assignees"]] == ["admin@alfa.invalid"]
        assert row["created_at"] == str(lab.first.created_at)

    def test_nested_order_lists_still_return_every_order(self, client, lab):
        by_patient = client.get(f"/api/v1/laboratory/patients/{lab.perez.id}/orders", headers=lab.headers).json()
        assert sorted(o["order_code"] for o in by_patient["orders"]) == ["BX-1", "CU-011026"]
        by_physician = client.get(f"/api/v1/requesting-physicians/{lab.salas.id}/orders", headers=lab.headers).json()
        assert [o["order_code"] for o in by_physician["orders"]] == ["CU-1"]

    @staticmethod
    def _count_statements(client, session, path, headers, params=None) -> int:
        counter = {"n": 0}

        def count(*_args, **_kwargs):
            counter["n"] += 1

        # Expire everything, as a fresh request would see it: otherwise rows
        # this test just inserted would answer `session.get` without SQL and
        # hide a per-row lookup.
        session.expire_all()
        bind = session.get_bind()
        event.listen(bind, "before_cursor_execute", count)
        try:
            assert client.get(path, params=params, headers=headers).status_code == 200
        finally:
            event.remove(bind, "before_cursor_execute", count)
        return counter["n"]

    @pytest.mark.parametrize("path", [SAMPLES, ORDERS, REPORTS])
    def test_statement_count_does_not_grow_with_rows(self, client, session, lab, path):
        def grow(n: int) -> None:
            label = lab.label(f"L{uuid4().hex[:6]}")
            for i in range(n):
                order = lab.order(f"GROW-{uuid4().hex[:6]}", patient=lab.perez)
                lab.sample(order, f"G{uuid4().hex[:8]}")
                lab.tag_order(order, label)
                lab.assign("lab_order", order.id, lab.admin)
                lab.report(order, f"R{i}")

        grow(3)
        few = self._count_statements(client, session, path, lab.headers)
        grow(12)
        many = self._count_statements(client, session, path, lab.headers)
        assert many == few

    def test_worklist_statement_count_does_not_grow_with_items(self, client, session, lab):
        def grow(n: int) -> None:
            for _ in range(n):
                order = lab.order(f"W-{uuid4().hex[:6]}", patient=lab.perez)
                lab.assign("lab_order", order.id, lab.admin)
                sample = lab.sample(order, f"W{uuid4().hex[:8]}")
                lab.assign("sample", sample.id, lab.admin)

        grow(2)
        few = self._count_statements(client, session, "/api/v1/me/worklist", lab.headers, {"page_size": 100})
        grow(10)
        many = self._count_statements(client, session, "/api/v1/me/worklist", lab.headers, {"page_size": 100})
        assert many == few
