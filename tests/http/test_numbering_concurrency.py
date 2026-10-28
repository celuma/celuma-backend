"""Concurrent allocation against real Postgres (issue #8).

Every request gets its own session/connection; threads start together behind
a barrier. The assertions are about outcomes: no duplicate identifier, no
failed request, no deadlock, no gaps where none should exist.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from collections import Counter

import pytest
from sqlalchemy import text

from .numbering_support import (  # noqa: F401
    ORDERS, SAMPLES, UNIFIED, Lab, configure, freeze, fresh_client, ledger_rows, order_codes, run_parallel, sample_codes, set_counter,
)


@pytest.fixture
def lab(session, monkeypatch):
    freeze(monkeypatch)
    lab = Lab(session, "Alfa", sample_types=("BX", "TJ"))
    configure(session, lab)
    return lab


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


@pytest.mark.parametrize("round_no", range(2))
def test_inactive_samples_use_the_compatibility_allocator_atomically(fresh_client, session, monkeypatch, round_no):
    """Never-activated laboratories: concurrent sample registrations of one type get unique, contiguous `<code>-<n>`."""
    freeze(monkeypatch)
    legacy = Lab(session, f"Legacy{round_no}", sample_types=("BX", "TJ"))  # no numbering configuration at all
    order = fresh_client.post(ORDERS, json=legacy.order_payload(), headers=legacy.headers).json()["id"]
    jobs = [lambda c: c.post(SAMPLES, json=legacy.sample_payload(order), headers=legacy.headers) for _ in range(10)]
    jobs += [lambda c: c.post(UNIFIED, json=legacy.unified(sample_codes=("BX", "TJ")), headers=legacy.headers) for _ in range(5)]
    results = run_parallel(jobs)
    all_ok(results)
    codes = sample_codes(session, legacy)
    assert len(codes) == len(set(codes)) == 20
    assert sorted(c for c in codes if c.startswith("BX-")) == sorted(f"BX-{n}" for n in range(1, 16))
    assert sorted(c for c in codes if c.startswith("TJ-")) == sorted(f"TJ-{n}" for n in range(1, 6))
    assert sorted(order_codes(session, legacy)) == sorted(f"CU-{n}" for n in range(1, 7))


def test_parallel_requests_across_tenants_are_isolated(fresh_client, session, lab):
    other = Lab(session, "Beta", sample_types=("BX",))
    configure(session, other)
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


def race_allocations(engine, lab, n=20):
    """N threads, each in its own transaction, allocate from ONE counter at the same instant."""
    from sqlmodel import Session

    from app.services import numbering

    barrier = threading.Barrier(n)
    out, errors = [], []

    def worker():
        try:
            with Session(engine) as s:
                barrier.wait(timeout=30)
                alloc = numbering.allocate_batch(
                    s, tenant_id=lab.tenant.id, instant=datetime(2026, 10, 15, 18, tzinfo=timezone.utc),
                    requests=[numbering.AllocationRequest("k", numbering.SAMPLE, lab.sample_types["BX"].id, "BX", received_at=datetime(2026, 10, 15))],  # #33: a sample is numbered by its receipt date
                    settings_by_kind={numbering.SAMPLE: numbering.NumberingSettings("sample", active=True)}, actor_id=None,
                )["k"]
                s.commit()
                out.append(alloc)
        except Exception as exc:  # a broken allocator may fail loudly too
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    return out, errors


def clean_race(out, errors, n):
    """True only for a perfectly atomic counter: N values 1..N, nobody had to skip, nobody failed."""
    return (not errors and sorted(a.sequence_value for a in out) == list(range(1, n + 1))
            and all(a.skipped == 0 for a in out))


def test_the_counter_alone_is_atomic_without_relying_on_collision_skips(engine, lab):
    """The ledger would hide a lost update by skipping, so assert no allocation had to skip."""
    out, errors = race_allocations(engine, lab)
    assert clean_race(out, errors, 20), (sorted(a.sequence_value for a in out), [a.skipped for a in out], errors)


def test_the_race_harness_DETECTS_a_non_atomic_counter(engine, lab, monkeypatch):
    """Mutation proof: swap the atomic upsert for a read-then-write (the classic lost update).
    The harness above must flag it; if it did not, `test_the_counter_alone_is_atomic...` would prove nothing."""
    from sqlalchemy import text

    from app.services import numbering

    racy = text(
        """
        WITH cur AS (
            SELECT COALESCE((SELECT last_value FROM numbering_counter WHERE tenant_id = :t AND entity_kind = :k
                AND scope_id = :s AND branch_id = :b AND period_year = :y AND period_month = :m), 0) AS v,
                pg_sleep(0.05)
        )
        INSERT INTO numbering_counter (tenant_id, entity_kind, scope_id, branch_id, period_year, period_month, last_value, updated_at)
        SELECT :t, :k, :s, :b, :y, :m, GREATEST(v + 1, :start), :now FROM cur
        ON CONFLICT (tenant_id, entity_kind, scope_id, branch_id, period_year, period_month)
        DO UPDATE SET last_value = EXCLUDED.last_value
        RETURNING last_value
        """
    )
    monkeypatch.setattr(numbering, "_BUMP_SQL", racy)
    out, errors = race_allocations(engine, lab)
    assert not [e for e in errors if isinstance(e, (TypeError, AttributeError, NameError))], errors  # a harness bug is not a detection
    assert not clean_race(out, errors, 20), "the harness failed to notice a lost update"
    assert any(a.skipped > 0 for a in out) or errors or len({a.sequence_value for a in out}) < len(out)
