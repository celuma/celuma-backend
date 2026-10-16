"""Concurrent allocation against real Postgres (issue #8).

Every request gets its own session/connection; threads start together behind
a barrier. The assertions are about outcomes: no duplicate identifier, no
failed request, no deadlock, no gaps where none should exist.
"""
from __future__ import annotations

import threading
from collections import Counter

import pytest
from sqlalchemy import text

from .numbering_support import (  # noqa: F401
    ORDERS, SAMPLES, UNIFIED, Lab, freeze, fresh_client, ledger_rows, new_client, order_codes, sample_codes, set_counter,
)


@pytest.fixture
def lab(session, monkeypatch):
    freeze(monkeypatch)
    return Lab(session, "Alfa", sample_types=("BX", "TJ"))


def run_parallel(jobs):
    """Run callables at the same time; returns their results in order."""
    barrier = threading.Barrier(len(jobs))
    results = [None] * len(jobs)

    def worker(i, job):
        client = new_client()
        barrier.wait(timeout=30)
        try:
            results[i] = job(client)
        except Exception as exc:  # surfaced by the assertions below
            results[i] = exc

    threads = [threading.Thread(target=worker, args=(i, j)) for i, j in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
        assert not t.is_alive(), "a worker hung (deadlock?)"
    return results


def all_ok(results):
    bad = [r for r in results if isinstance(r, Exception) or r.status_code != 200]
    assert not bad, [getattr(r, "text", r) for r in bad]


def test_parallel_orders_in_one_scope_get_unique_contiguous_numbers(fresh_client, session, lab):
    jobs = [lambda c: c.post(ORDERS, json=lab.order_payload(), headers=lab.headers) for _ in range(16)]
    results = run_parallel(jobs)
    all_ok(results)
    codes = sorted(r.json()["order_code"] for r in results)
    assert codes == sorted(f"CU{n:02d}1026" for n in range(1, 17))
    assert ledger_rows(session) == 16


def test_parallel_samples_of_one_type_are_unique_and_contiguous(fresh_client, session, lab):
    order = fresh_client.post(ORDERS, json=lab.order_payload(), headers=lab.headers).json()["id"]
    jobs = [lambda c: c.post(SAMPLES, json=lab.sample_payload(order), headers=lab.headers) for _ in range(12)]
    results = run_parallel(jobs)
    all_ok(results)
    assert sorted(r.json()["sample_code"] for r in results) == sorted(f"BX{n:02d}1026" for n in range(1, 13))


def test_unified_requests_with_opposite_sample_type_order_do_not_deadlock(fresh_client, session, lab):
    forward, backward = ("BX", "TJ"), ("TJ", "BX")
    jobs = [
        (lambda c, kinds=(forward if i % 2 == 0 else backward): c.post(UNIFIED, json=lab.unified(sample_codes=kinds), headers=lab.headers))
        for i in range(12)
    ]
    results = run_parallel(jobs)
    all_ok(results)
    orders = sorted(r.json()["order"]["order_code"] for r in results)
    assert orders == sorted(f"CU{n:02d}1026" for n in range(1, 13))
    by_type = Counter()
    seen = set()
    for r in results:
        for s in r.json()["samples"]:
            assert s["sample_code"] not in seen
            seen.add(s["sample_code"])
            by_type[s["sample_code"][:2]] += 1
    assert by_type == {"BX": 12, "TJ": 12}
    assert sorted(c for c in seen if c.startswith("BX")) == sorted(f"BX{n:02d}1026" for n in range(1, 13))


def test_scopes_that_render_the_same_string_never_duplicate(fresh_client, session, lab):
    x1, x = lab.add_study_type("X1"), lab.add_study_type("X")
    set_counter(session, lab, "order", x, 100)  # X101 and X1 01 render the same text
    jobs = [lambda c: c.post(UNIFIED, json=lab.unified(study_type=x1), headers=lab.headers) for _ in range(6)]
    jobs += [lambda c: c.post(UNIFIED, json=lab.unified(study_type=x), headers=lab.headers) for _ in range(6)]
    results = run_parallel(jobs)
    all_ok(results)
    codes = [r.json()["order"]["order_code"] for r in results]
    assert len(set(codes)) == 12
    assert len(set(order_codes(session, lab))) == 12


@pytest.mark.parametrize("round_no", range(4))
def test_manual_and_generated_codes_cannot_both_take_the_same_string(fresh_client, session, lab, round_no):
    fresh_client.post(ORDERS, json=lab.order_payload(), headers=lab.headers)  # counter at 1, next is CU021026
    manual_code = "CU021026"
    jobs = [
        lambda c: c.post(UNIFIED, json=lab.unified(order_code=manual_code), headers=lab.headers),
        lambda c: c.post(UNIFIED, json=lab.unified(), headers=lab.headers),
        lambda c: c.post(UNIFIED, json=lab.unified(), headers=lab.headers),
    ]
    results = run_parallel(jobs)
    assert not [r for r in results if isinstance(r, Exception)]
    assert all(r.status_code in (200, 400) for r in results), [r.text for r in results]
    codes = order_codes(session, lab)
    assert len(codes) == len(set(codes)), codes  # nobody holds a duplicate, whoever won
    assert codes.count(manual_code) <= 1


def test_parallel_requests_across_tenants_are_isolated(fresh_client, session, lab):
    other = Lab(session, "Beta", sample_types=("BX",))
    jobs = [lambda c: c.post(UNIFIED, json=lab.unified(), headers=lab.headers) for _ in range(5)]
    jobs += [lambda c: c.post(UNIFIED, json=other.unified(), headers=other.headers) for _ in range(5)]
    results = run_parallel(jobs)
    all_ok(results)
    assert order_codes(session, lab).__len__() == 5 and sorted(order_codes(session, lab)) == sorted(order_codes(session, other))
    assert sorted(sample_codes(session, lab)) == sorted(sample_codes(session, other))


def test_a_failed_parallel_request_does_not_burn_a_number_for_the_others(fresh_client, session, lab):
    bad = lab.unified(sample_codes=("BX",))
    bad["samples"][0]["sample_type_id"] = "00000000-0000-0000-0000-000000000000"  # 404 before any allocation
    jobs = [lambda c: c.post(UNIFIED, json=lab.unified(), headers=lab.headers) for _ in range(4)]
    jobs.append(lambda c: c.post(UNIFIED, json=bad, headers=lab.headers))
    results = run_parallel(jobs)
    assert [r.status_code for r in results[:4]] == [200] * 4 and results[4].status_code == 404
    assert sorted(order_codes(session, lab)) == [f"CU{n:02d}1026" for n in range(1, 5)]
    assert session.execute(text("SELECT sum(last_value) FROM numbering_counter WHERE entity_kind = 'order'")).scalar() == 4


def test_the_counter_alone_is_atomic_without_relying_on_collision_skips(engine, lab):
    """Direct service-level race: the ledger would hide a lost update by
    skipping, so assert no allocation had to skip and values are 1..N."""
    from sqlmodel import Session

    from app.services import numbering

    n = 20
    barrier = threading.Barrier(n)
    out = []

    def worker():
        with Session(engine) as s:
            barrier.wait(timeout=30)
            alloc = numbering.allocate_batch(
                s, tenant_id=lab.tenant.id, period=numbering.Period(2026, 10),
                requests=[numbering.AllocationRequest("k", numbering.SAMPLE, lab.sample_types["BX"].id, "BX")],
                settings_by_kind={numbering.SAMPLE: numbering.NumberingSettings("sample")}, actor_id=None,
            )["k"]
            s.commit()
            out.append(alloc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert sorted(a.sequence_value for a in out) == list(range(1, n + 1))
    assert all(a.skipped == 0 for a in out)
