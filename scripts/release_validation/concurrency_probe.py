"""Observe locks and concurrent-writer behaviour during the migration (§15).

TEST TOOLING ONLY. Never imported from `app/`.

Run this in a second process while `run_profile.py` migrates the same
database. It plays the part of a Céluma 1.2 application task that is still
serving traffic during an ECS rolling deploy: it reads, inserts and updates
`storage_object` using only columns that exist at `v1_2_0`, and records
whether each attempt succeeded, how long it waited, and what it waited on.

Nothing here is destructive. Inserts go to synthetic rows the probe owns and
updates touch only those rows, so the migration's own backfill (guarded by
`tenant_id IS NULL`) and the integrity checks are unaffected -- the probe's
rows are counted separately in the report.

    python scripts/release_validation/concurrency_probe.py relval_medium --seconds 120
"""
from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras

psycopg2.extras.register_uuid()

DB_HOST = os.environ.get("RELVAL_DB_HOST", "celuma-relval-db")
DB_PORT = os.environ.get("RELVAL_DB_PORT", "5432")
DB_USER = os.environ.get("RELVAL_DB_USER", "postgres")
DB_PASS = os.environ.get("RELVAL_DB_PASS", "postgres")
OUT_DIR = Path(os.environ.get("RELVAL_OUT", "/app/.release-validation"))

#: Bound every probe statement so a probe that blocks reports a wait instead
#: of hanging the run. Longer than any healthy statement, far shorter than a
#: migration.
STATEMENT_TIMEOUT_MS = 15_000

PROBE_KEY_PREFIX = "probe/concurrent-writer"


def connect(database: str):
    conn = psycopg2.connect(
        f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{database}")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
    return conn


def timed(cur, sql: str, args: tuple = ()) -> dict:
    """Run one statement, recording latency and any error rather than raising."""
    started = time.monotonic()
    try:
        cur.execute(sql, args) if args else cur.execute(sql)
        return {"ok": True, "seconds": round(time.monotonic() - started, 4)}
    except Exception as exc:  # noqa: BLE001 - the failure IS the measurement
        return {"ok": False, "seconds": round(time.monotonic() - started, 4),
                "error": type(exc).__name__, "message": str(exc).strip()[:200]}


def sample_activity(conn, database: str) -> dict:
    """One observation of what the server is doing and what is blocked."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pid, state, wait_event_type, wait_event,
                   left(regexp_replace(query, '\\s+', ' ', 'g'), 120),
                   now() - xact_start
            FROM pg_stat_activity
            WHERE datname = %s AND pid <> pg_backend_pid() AND state <> 'idle'
        """, (database,))
        activity = [
            {"pid": p, "state": s, "wait_event_type": wt, "wait_event": we,
             "query": q, "xact_age": str(age)}
            for p, s, wt, we, q, age in cur.fetchall()
        ]
        cur.execute("""
            SELECT l.mode, l.granted, c.relname
            FROM pg_locks l
            LEFT JOIN pg_class c ON c.oid = l.relation
            JOIN pg_stat_activity a ON a.pid = l.pid
            WHERE a.datname = %s AND l.pid <> pg_backend_pid()
              AND c.relname IS NOT NULL
        """, (database,))
        locks = [{"mode": m, "granted": g, "relation": r} for m, g, r in cur.fetchall()]
    return {"at": datetime.now(timezone.utc).isoformat(),
            "activity": activity, "locks": locks}


def observe_only(args) -> int:
    """Sample the server continuously, issuing no statements of our own.

    Records every lock the migration takes on every relation, and every
    backend waiting on one, so the blocking is evidence rather than inference.
    """
    observer = connect(args.database)
    observations = []
    lock_modes: dict[str, set] = {}
    waiters: list[dict] = []

    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        obs = sample_activity(observer, args.database)
        observations.append(obs)
        for lock in obs["locks"]:
            lock_modes.setdefault(lock["relation"], set()).add(
                (lock["mode"], lock["granted"]))
            if not lock["granted"]:
                waiters.append({"at": obs["at"], **lock})
        for act in obs["activity"]:
            if act["wait_event_type"] == "Lock":
                waiters.append({"at": obs["at"], "waiting_query": act["query"],
                                "wait_event": act["wait_event"]})
        time.sleep(args.interval)

    out = {
        "database": args.database,
        "mode": "observe-only",
        "samples": len(observations),
        "lock_modes_by_relation": {
            rel: sorted([f"{m}{'' if g else ' (WAITING)'}" for m, g in modes])
            for rel, modes in sorted(lock_modes.items())
        },
        "blocked_events": waiters[:60],
        "blocked_event_count": len(waiters),
        "observations": observations,
    }
    path = Path(args.out) if args.out else OUT_DIR / f"{args.database}-locks.json"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2, default=str))
    print(json.dumps({k: out[k] for k in
                      ("samples", "lock_modes_by_relation", "blocked_event_count")},
                     indent=2))
    print(f"lock report: {path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("database")
    ap.add_argument("--seconds", type=float, default=120.0)
    ap.add_argument("--interval", type=float, default=0.25)
    ap.add_argument("--out", default=None)
    # The writer blocks for the whole migration, and a blocked writer takes no
    # observations. Running the observer as its own process is what makes the
    # ungranted lock visible while it is still ungranted.
    ap.add_argument("--observe-only", action="store_true",
                    help="sample pg_locks/pg_stat_activity only; issue no writes")
    args = ap.parse_args()

    if args.observe_only:
        return observe_only(args)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else OUT_DIR / f"{args.database}-concurrency.json"

    probe = connect(args.database)
    observer = connect(args.database)

    # A row the probe owns, created before the migration starts, so the UPDATE
    # path has a stable target. Uses 1.2-era columns only.
    seed_id = uuid.uuid4()
    with probe.cursor() as cur:
        cur.execute("""
            INSERT INTO storage_object
                (created_at, id, provider, region, bucket, object_key, etag,
                 content_type, size_bytes)
            VALUES (now(), %s, 'aws', 'mx-central-1', 'celuma-media-synthetic',
                    %s, 'seed', 'image/jpeg', 1234)
        """, (seed_id, f"{PROBE_KEY_PREFIX}/seed.jpg"))

    results = {"database": args.database, "seed_row": str(seed_id),
               "started_at": datetime.now(timezone.utc).isoformat(),
               "attempts": [], "observations": [], "summary": {}}

    deadline = time.monotonic() + args.seconds
    n = 0
    while time.monotonic() < deadline:
        n += 1
        with probe.cursor() as cur:
            select = timed(cur, "SELECT COUNT(*) FROM storage_object WHERE object_key LIKE %s",
                           (f"{PROBE_KEY_PREFIX}%",))
            insert = timed(cur, """
                INSERT INTO storage_object
                    (created_at, id, provider, region, bucket, object_key, etag,
                     content_type, size_bytes)
                VALUES (now(), %s, 'aws', 'mx-central-1', 'celuma-media-synthetic',
                        %s, 'probe', 'image/jpeg', 4096)
            """, (uuid.uuid4(), f"{PROBE_KEY_PREFIX}/{n:06d}.jpg"))
            update = timed(cur, "UPDATE storage_object SET etag = %s WHERE id = %s",
                           (f"probe-{n}", seed_id))
            # An unrelated table, to show whether the migration's locks are
            # confined to storage_object.
            unrelated = timed(cur, "SELECT COUNT(*) FROM patient")

        results["attempts"].append({
            "n": n, "at": datetime.now(timezone.utc).isoformat(),
            "select": select, "insert": insert, "update": update,
            "unrelated_select": unrelated,
        })
        results["observations"].append(sample_activity(observer, args.database))
        time.sleep(args.interval)

    def stats(kind: str) -> dict:
        vals = [a[kind]["seconds"] for a in results["attempts"]]
        failures = [a[kind] for a in results["attempts"] if not a[kind]["ok"]]
        return {
            "attempts": len(vals),
            "succeeded": len(vals) - len(failures),
            "failed": len(failures),
            "max_seconds": round(max(vals), 4) if vals else None,
            "mean_seconds": round(sum(vals) / len(vals), 4) if vals else None,
            "first_error": failures[0] if failures else None,
        }

    results["summary"] = {k: stats(k) for k in ("select", "insert", "update", "unrelated_select")}
    results["finished_at"] = datetime.now(timezone.utc).isoformat()
    results["probe_rows_inserted"] = n
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(json.dumps(results["summary"], indent=2))
    print(f"probe report: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
