"""`locate` on the order and sample lists (Céluma 1.5, #66).

A list re-opened after visiting a record (browser Back or the in-app return)
asks whether that record still matches the same search and filters and on
which page the same ordering now puts it. Contract:
`celuma-engineering/specs/lists/search-and-pagination.md` ("Returning to a
list"). Synthetic tenants only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from .test_list_search_and_pagination import ORDERS, SAMPLES, Lab


@pytest.fixture
def lab(session):
    lab = Lab(session, "Locate")
    lab.patients = [lab.patient(f"Paciente{i}", "Sintético", f"P-{i}") for i in range(3)]
    # 12 orders: CU-01..CU-12, alternating status; each with one sample S-01..S-12.
    lab.orders = []
    lab.samples = []
    for i in range(1, 13):
        order = lab.order(f"CU-{i:02d}", patient=lab.patients[i % 3],
                          status="RECEIVED" if i % 2 else "PROCESSING")
        lab.orders.append(order)
        lab.samples.append(lab.sample(order, f"S-{i:02d}", state="RECEIVED" if i % 2 else "PROCESSING"))
    return lab


def get(client, lab, path, **params):
    response = client.get(path, params=params, headers=lab.headers)
    assert response.status_code == 200, response.text
    return response.json()


class TestLocateOrders:
    def test_a_matching_order_reports_its_position_and_page(self, client, lab):
        target = lab.orders[6]  # CU-07
        body = get(client, lab, ORDERS, page=1, page_size=5, locate=str(target.id))
        assert body["located"] == {"id": str(target.id), "matches": True, "position": 7, "page": 2}
        page = get(client, lab, ORDERS, page=2, page_size=5)
        assert str(target.id) in [row["id"] for row in page["orders"]]

    def test_the_position_follows_sort_direction_and_filters(self, client, lab):
        target = lab.orders[6]  # CU-07, RECEIVED: the 4th RECEIVED order ascending, 3rd descending
        body = get(client, lab, ORDERS, page=1, page_size=2, status="RECEIVED", sort="order_code", order="desc",
                   locate=str(target.id))
        assert body["located"] == {"id": str(target.id), "matches": True, "position": 3, "page": 2}
        page = get(client, lab, ORDERS, page=2, page_size=2, status="RECEIVED", sort="order_code", order="desc")
        assert [row["order_code"] for row in page["orders"]] == ["CU-07", "CU-05"]

    def test_an_order_whose_status_changed_no_longer_matches(self, client, session, lab):
        target = lab.orders[0]  # CU-01, RECEIVED
        before = get(client, lab, ORDERS, page=1, page_size=5, status="RECEIVED", locate=str(target.id))
        assert before["located"]["matches"] is True
        target.status = "DIAGNOSIS"
        lab.add(target)
        after = get(client, lab, ORDERS, page=1, page_size=5, status="RECEIVED", locate=str(target.id))
        assert after["located"] == {"id": str(target.id), "matches": False, "position": None, "page": None}
        # Without the filter it is still there.
        assert get(client, lab, ORDERS, page=1, page_size=5, locate=str(target.id))["located"]["matches"] is True

    def test_the_search_is_applied(self, client, lab):
        target = lab.orders[3]  # CU-04, patient P-1
        assert get(client, lab, ORDERS, page=1, page_size=5, q="P-1", locate=str(target.id))["located"]["matches"]
        assert not get(client, lab, ORDERS, page=1, page_size=5, q="P-2", locate=str(target.id))["located"]["matches"]

    def test_another_tenants_removed_and_unknown_orders_do_not_match(self, client, session, lab):
        other = Lab(session, "Ajeno")
        foreign = other.order("CU-01")
        removed = lab.orders[1]
        removed.removed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        lab.add(removed)
        for target in (foreign.id, removed.id, uuid4()):
            body = get(client, lab, ORDERS, page=1, page_size=5, locate=str(target))
            assert body["located"] == {"id": str(target), "matches": False, "position": None, "page": None}

    def test_the_page_itself_is_unchanged(self, client, lab):
        plain = get(client, lab, ORDERS, page=2, page_size=5, sort="patient")
        located = get(client, lab, ORDERS, page=2, page_size=5, sort="patient", locate=str(lab.orders[0].id))
        assert located["orders"] == plain["orders"] and located["total"] == plain["total"] == 12
        assert plain["located"] is None

    @pytest.mark.parametrize("params", [{"locate": "not-a-uuid", "page": 1}, {"locate": str(uuid4())}])
    def test_invalid_or_unpaged_locate_is_rejected(self, client, lab, params):
        response = client.get(ORDERS, params=params, headers=lab.headers)
        assert response.status_code == 422, response.text


class TestLocateSamples:
    def test_a_matching_sample_reports_its_page(self, client, lab):
        target = lab.samples[10]  # S-11: 11th ascending
        body = get(client, lab, SAMPLES, page=1, page_size=4, locate=str(target.id))
        assert body["located"] == {"id": str(target.id), "matches": True, "position": 11, "page": 3}
        page = get(client, lab, SAMPLES, page=3, page_size=4)
        assert str(target.id) in [row["id"] for row in page["samples"]]

    def test_a_sample_whose_state_changed_no_longer_matches(self, client, lab):
        target = lab.samples[1]  # S-02, PROCESSING
        assert get(client, lab, SAMPLES, page=1, page_size=4, state="PROCESSING",
                   locate=str(target.id))["located"]["matches"] is True
        target.state = "READY"
        lab.add(target)
        body = get(client, lab, SAMPLES, page=1, page_size=4, state="PROCESSING", locate=str(target.id))
        assert body["located"]["matches"] is False
        assert body["total"] == 5

    def test_unpaged_locate_is_rejected(self, client, lab):
        response = client.get(SAMPLES, params={"locate": str(lab.samples[0].id)}, headers=lab.headers)
        assert response.status_code == 422, response.text
