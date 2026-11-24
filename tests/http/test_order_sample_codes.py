"""Sample codes in the orders list (Céluma 1.5, #80).

Each order row carries the codes of its visible samples (`sample_codes`), and
the orders search finds an order by any one of them, across the whole
paginated collection, with one row per order. Contract:
`celuma-engineering/specs/lists/search-and-pagination.md` (orders searchable
fields and "Sample codes in the orders list"). Synthetic tenants only.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from .test_list_search_and_pagination import ORDERS, Lab


def get(client, lab, **params):
    response = client.get(ORDERS, params=params, headers=lab.headers)
    assert response.status_code == 200, response.text
    return response.json()


def order_codes(body) -> list[str]:
    return [row["order_code"] for row in body["orders"]]


def row_of(body, order_code: str) -> dict:
    return next(row for row in body["orders"] if row["order_code"] == order_code)


@pytest.fixture
def lab(session):
    lab = Lab(session, "Codigos")
    lab.perez = lab.patient("Juan", "Pérez", "P-1")
    lab.munoz = lab.patient("Lorena", "Muñóz", "P-2")
    # One sample.
    lab.single = lab.order("ORD-01", patient=lab.perez)
    lab.sample(lab.single, "CU276262")
    # Several samples; the later codes (in display order) are the ones searched below.
    lab.several = lab.order("ORD-02", patient=lab.munoz)
    for code in ("IHQ276262", "BX276262", "TJ-55-01"):
        lab.sample(lab.several, code)
    # A neighbouring code on a different order.
    lab.neighbour = lab.order("ORD-03", patient=lab.perez, status="PROCESSING")
    lab.sample(lab.neighbour, "CU276263")
    # A legacy order without samples.
    lab.legacy = lab.order("ORD-04", patient=lab.munoz)
    return lab


class TestSampleCodesColumn:
    def test_each_row_carries_its_visible_sample_codes_in_code_order(self, client, lab):
        body = get(client, lab, page=1, page_size=20)
        assert row_of(body, "ORD-01")["sample_codes"] == ["CU276262"]
        assert row_of(body, "ORD-02")["sample_codes"] == ["BX276262", "IHQ276262", "TJ-55-01"]
        assert row_of(body, "ORD-03")["sample_codes"] == ["CU276263"]

    def test_a_legacy_order_without_samples_has_an_empty_list(self, client, lab):
        row = row_of(get(client, lab, page=1, page_size=20), "ORD-04")
        assert row["sample_codes"] == [] and row["sample_count"] == 0

    def test_the_count_and_the_codes_agree(self, client, lab):
        lab.sample(lab.single, "CU276262")  # a corrected capture may repeat a code: two samples, two entries
        for row in get(client, lab, page=1, page_size=20)["orders"]:
            assert row["sample_count"] == len(row["sample_codes"])
        assert row_of(get(client, lab, page=1, page_size=20), "ORD-01")["sample_codes"] == ["CU276262", "CU276262"]

    def test_compatibility_mode_and_nested_order_lists_carry_the_codes(self, client, lab):
        whole = client.get(ORDERS, headers=lab.headers).json()
        assert row_of(whole, "ORD-02")["sample_codes"] == ["BX276262", "IHQ276262", "TJ-55-01"]
        nested = client.get(f"/api/v1/laboratory/patients/{lab.munoz.id}/orders", headers=lab.headers).json()
        assert row_of(nested, "ORD-02")["sample_codes"] == ["BX276262", "IHQ276262", "TJ-55-01"]
        assert row_of(nested, "ORD-04")["sample_codes"] == []


class TestSearchBySampleCode:
    def test_the_only_sample_code_finds_its_order(self, client, lab):
        body = get(client, lab, page=1, q="CU276262")
        assert order_codes(body) == ["ORD-01"] and body["total"] == 1

    @pytest.mark.parametrize("query", ["IHQ276262", "TJ-55-01", "tj5501", "Tj.55.01"])
    def test_a_later_code_in_the_cell_finds_its_order(self, client, lab, query):
        body = get(client, lab, page=1, q=query)
        assert order_codes(body) == ["ORD-02"] and body["total"] == 1

    def test_similar_codes_on_different_orders_do_not_match_each_other(self, client, lab):
        assert order_codes(get(client, lab, page=1, q="CU276262")) == ["ORD-01"]
        assert order_codes(get(client, lab, page=1, q="CU276263")) == ["ORD-03"]
        # A partial identifier is a literal substring: both neighbours, nothing else.
        assert sorted(order_codes(get(client, lab, page=1, q="CU27626"))) == ["ORD-01", "ORD-03"]
        assert get(client, lab, page=1, q="CU276264")["total"] == 0  # identifiers are never typo-corrected

    def test_several_matching_samples_of_one_order_give_one_row(self, client, lab):
        # "276262" is in CU276262 (ORD-01) and in both IHQ276262 and BX276262 (ORD-02).
        body = get(client, lab, page=1, q="276262")
        assert sorted(order_codes(body)) == ["ORD-01", "ORD-02"] and body["total"] == 2

    def test_a_term_never_spans_two_sample_codes(self, client, lab):
        # Joined, BX276262 + IHQ276262 would contain "62ihq"; each code is matched on its own.
        assert get(client, lab, page=1, q="62IHQ")["total"] == 0

    def test_sample_code_and_other_fields_are_and_matched(self, client, lab):
        assert order_codes(get(client, lab, page=1, q="munoz 276262")) == ["ORD-02"]
        assert order_codes(get(client, lab, page=1, q="perez 276262")) == ["ORD-01"]
        assert get(client, lab, page=1, q="perez IHQ276262")["total"] == 0

    def test_existing_fields_keep_matching(self, client, lab):
        assert order_codes(get(client, lab, page=1, q="ORD-04")) == ["ORD-04"]
        assert sorted(order_codes(get(client, lab, page=1, q="P-2"))) == ["ORD-02", "ORD-04"]


class TestAcrossPagesAndFilters:
    @pytest.fixture
    def many(self, lab):
        # 25 more orders with one sample each, so the targets live on late pages.
        for i in range(25):
            order = lab.order(f"ORD-1{i:02d}", patient=lab.perez, status="RECEIVED" if i % 2 else "PROCESSING")
            lab.sample(order, f"MX{i:02d}0001")
        lab.late = lab.order("ORD-999", patient=lab.munoz)
        for code in ("AA-0001", "ZZ-424242"):
            lab.sample(lab.late, code)
        return lab

    def test_a_code_finds_an_order_that_is_not_on_the_first_unfiltered_page(self, client, many):
        unfiltered = get(client, many, page=1, page_size=10)
        assert "ORD-999" not in order_codes(unfiltered) and unfiltered["total"] == 30
        body = get(client, many, page=1, page_size=10, q="ZZ-424242")
        assert order_codes(body) == ["ORD-999"] and body["total"] == 1

    def test_paging_through_a_code_search_returns_each_order_once(self, client, many):
        # "0001" is in every MX..0001 code and in AA-0001: 26 orders, ORD-999 with one matching sample.
        seen, total = [], None
        for page in (1, 2, 3):
            body = get(client, many, page=page, page_size=10, q="0001")
            seen += order_codes(body)
            total = body["total"]
        assert total == 26 and len(seen) == 26 and len(set(seen)) == 26
        past_the_end = get(client, many, page=4, page_size=10, q="0001")
        assert past_the_end["orders"] == [] and past_the_end["total"] == 26

    def test_filters_and_sort_apply_to_a_code_search(self, client, many):
        received = get(client, many, page=1, page_size=100, q="0001", status="RECEIVED")
        assert received["total"] == 13  # 12 MX orders + ORD-999
        assert all(row["status"] == "RECEIVED" for row in received["orders"])
        processing = get(client, many, page=1, page_size=100, q="ZZ-424242", status="PROCESSING")
        assert processing["total"] == 0
        desc = get(client, many, page=1, page_size=3, q="0001", sort="order_code", order="desc")
        assert order_codes(desc) == ["ORD-999", "ORD-124", "ORD-123"]
        by_patient = get(client, many, page=1, page_size=100, q="0001", patient_id=str(many.munoz.id))
        assert order_codes(by_patient) == ["ORD-999"]

    def test_locate_follows_the_code_search(self, client, many):
        body = get(client, many, page=1, page_size=10, q="0001", sort="order_code", order="desc",
                   locate=str(many.late.id))
        assert body["located"] == {"id": str(many.late.id), "matches": True, "position": 1, "page": 1}
        missing = get(client, many, page=1, page_size=10, q="ZZ-424242", locate=str(many.single.id))
        assert missing["located"]["matches"] is False


class TestVisibility:
    def test_a_removed_sample_is_neither_shown_nor_searchable(self, client, session, lab):
        removed = lab.sample(lab.several, "RM-777")
        assert order_codes(get(client, lab, page=1, q="RM-777")) == ["ORD-02"]
        removed.removed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        lab.add(removed)
        assert get(client, lab, page=1, q="RM-777")["total"] == 0
        row = row_of(get(client, lab, page=1, page_size=20), "ORD-02")
        assert row["sample_codes"] == ["BX276262", "IHQ276262", "TJ-55-01"] and row["sample_count"] == 3

    def test_another_laboratorys_sample_codes_find_nothing_here(self, client, session, lab):
        other = Lab(session, "Ajeno")
        foreign = other.order("EXT-01")
        other.sample(foreign, "CU276262")
        other.sample(foreign, "XT-31337")
        assert order_codes(get(client, lab, page=1, q="CU276262")) == ["ORD-01"]
        assert get(client, lab, page=1, q="XT-31337")["total"] == 0
        # And the other laboratory sees only its own order for the shared code.
        assert order_codes(get(client, other, page=1, q="CU276262")) == ["EXT-01"]

    def test_a_sample_whose_laboratory_differs_from_its_orders_is_neither_shown_nor_searchable(
        self, client, session, lab
    ):
        # Inconsistent data: display and search apply the same tenant condition.
        other = Lab(session, "Cruzado")
        stray = other.sample(lab.single, "XT-99999")
        assert stray.order_id == lab.single.id
        assert get(client, lab, page=1, q="XT-99999")["total"] == 0
        assert row_of(get(client, lab, page=1, page_size=20), "ORD-01")["sample_codes"] == ["CU276262"]
