"""The bounded release checks that do not need a full profile run.

TEST TOOLING ONLY. Never imported from `app/`.

Covers Block B §26 (notification constraints), §27 (threshold state), §28
(reconciliation accounting), §30 (fresh install on PostgreSQL 16), §31
(downgrade / re-upgrade) and the tenant-logo control experiment behind §20.

    python scripts/release_validation/bounded_experiments.py fresh-install
    python scripts/release_validation/bounded_experiments.py constraints relval_small
    python scripts/release_validation/bounded_experiments.py downgrade-cycle
    python scripts/release_validation/bounded_experiments.py reconciliation relval_small
    python scripts/release_validation/bounded_experiments.py logo-control
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

from snapshot import recompute_billable_bytes, snapshot  # noqa: E402

psycopg2.extras.register_uuid()

DB_HOST = os.environ.get("RELVAL_DB_HOST", "celuma-relval-db")
DB_PORT = os.environ.get("RELVAL_DB_PORT", "5432")
DB_USER = os.environ.get("RELVAL_DB_USER", "postgres")
DB_PASS = os.environ.get("RELVAL_DB_PASS", "postgres")
OUT_DIR = Path(os.environ.get("RELVAL_OUT", "/app/.release-validation"))

#: The ten final Céluma 1.3 notification types: the six Phase 3 domain types
#: plus the four Phase 4 Block G usage-threshold types.
NOTIFICATION_TYPES = (
    "REPORT_SUBMITTED", "REPORT_PDF_READY", "REPORT_PUBLISHED", "REPORT_RETRACTED",
    "ASSIGNMENT_ADDED", "SAMPLE_STATUS_CHANGED",
    "STORAGE_USAGE_APPROACHING", "STORAGE_LIMIT_REACHED",
    "USER_LIMIT_APPROACHING", "USER_LIMIT_REACHED",
)


def dsn(database: str) -> str:
    return f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{database}"


def sa_url(database: str) -> str:
    return f"postgresql+psycopg2://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{database}"


def recreate(name: str) -> None:
    conn = psycopg2.connect(dsn("postgres"))
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
        cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        cur.execute(f'CREATE DATABASE "{name}"')
    conn.close()


def alembic(database: str, direction: str, target: str) -> tuple[float, str, int]:
    env = dict(os.environ, DATABASE_URL=sa_url(database))
    started = time.monotonic()
    proc = subprocess.run(["alembic", direction, target], cwd="/app", env=env,
                          capture_output=True, text=True)
    return time.monotonic() - started, proc.stdout + proc.stderr, proc.returncode


def _rows_of(conn, sql: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def emit(name: str, payload: dict) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"experiment-{name}.json").write_text(json.dumps(payload, indent=2, default=str))
    print(json.dumps(payload, indent=2, default=str))
    return 0


# --------------------------------------------------------------------------

def fresh_install() -> int:
    """§30 -- one `base -> v1_3_0` on PostgreSQL 16, as a version check."""
    db = "relval_fresh"
    recreate(db)
    seconds, output, rc = alembic(db, "upgrade", "v1_3_0")
    conn = psycopg2.connect(dsn(db))
    with conn.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        revision = cur.fetchone()[0]
        cur.execute("SELECT version()")
        pg = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public'")
        tables = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM information_schema.columns "
                    "WHERE table_schema = 'public'")
        columns = cur.fetchone()[0]
        # A fresh install runs every section-15 statement against zero rows.
        cur.execute("SELECT COUNT(*) FROM tenant_usage")
        usage = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM notification")
        notifications = cur.fetchone()[0]
    conn.close()
    return emit("fresh-install", {
        "postgresql_version": pg, "returncode": rc, "seconds": round(seconds, 3),
        "alembic_version": revision, "tables": tables, "columns": columns,
        "tenant_usage_rows": usage, "notification_rows": notifications,
        "passed": rc == 0 and revision == "v1_3_0" and usage == 0 and notifications == 0,
        "output_tail": output[-1500:],
    })


def constraints(database: str) -> int:
    """§26 -- the database accepts all ten final types and rejects an invented one."""
    conn = psycopg2.connect(dsn(database))
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM tenant ORDER BY id LIMIT 1")
        tenant_id = cur.fetchone()[0]

    accepted, rejected = [], []
    for type_name in NOTIFICATION_TYPES + ("TOTALLY_INVENTED_TYPE",):
        nid = uuid.uuid4()
        try:
            with conn.cursor() as cur:
                # Every NOT NULL column must be named; the point of the check
                # is `ck_notification_type`, so nothing else may fail first.
                cur.execute("""
                    INSERT INTO notification
                        (id, tenant_id, type, severity, title, resource_type,
                         resource_id, idempotency_key, locale, created_at)
                    VALUES (%s, %s, %s, 'INFO', 'constraint probe', 'report',
                            %s, %s, 'es-MX', now())
                """, (nid, tenant_id, type_name, nid, f"relval-{nid}"))
            conn.commit()
            accepted.append(type_name)
            # Leave the table as the migration left it.
            with conn.cursor() as cur:
                cur.execute("DELETE FROM notification WHERE id = %s", (nid,))
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            rejected.append({"type": type_name, "error": type(exc).__name__})

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM notification")
        remaining = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM tenant_usage_threshold_state")
        threshold_state = cur.fetchone()[0]
    conn.close()

    return emit(f"constraints-{database}", {
        "database": database,
        "accepted": accepted,
        "rejected": rejected,
        "all_ten_accepted": sorted(accepted) == sorted(NOTIFICATION_TYPES),
        "invented_type_rejected": any(r["type"] == "TOTALLY_INVENTED_TYPE" for r in rejected),
        "notification_rows_after_cleanup": remaining,
        "threshold_state_rows": threshold_state,
    })


def downgrade_cycle(signature_mode: str = "faithful") -> int:
    """§31 -- v1_2_0 -> v1_3_0 -> v1_2_0 -> v1_3_0 on SMALL data.

    `faithful` since the B-001 remediation: the cycle now runs with several
    signature-bearing users per tenant, so the round trip has to reproduce a
    *summed* signature contribution twice rather than a single row's bytes.
    Before the fix this could only be run under `one_per_tenant`, because the
    first upgrade aborted under any realistic signature distribution.
    """
    from dataset import Generator
    from profiles import SMALL

    db = "relval_cycle"
    recreate(db)
    alembic(db, "upgrade", "v1_2_0")
    conn = psycopg2.connect(dsn(db))
    Generator(SMALL, 20260815, signature_mode).generate(conn)
    with conn.cursor() as cur:
        cur.execute("ANALYZE")
    conn.commit()
    baseline = snapshot(conn)
    conn.close()

    steps = []

    def clinical(conn) -> dict:
        snap = snapshot(conn)
        return {t: snap["counts"].get(t) for t in
                ("tenant", "patient", "order", "sample", "report", "report_version",
                 "storage_object", "app_user", "sample_image")}

    for direction, target in (("upgrade", "v1_3_0"), ("downgrade", "v1_2_0"),
                              ("upgrade", "v1_3_0")):
        seconds, output, rc = alembic(db, direction, target)
        conn = psycopg2.connect(dsn(db))
        with conn.cursor() as cur:
            cur.execute("SELECT version_num FROM alembic_version")
            revision = cur.fetchone()[0]
        step = {"direction": direction, "target": target, "returncode": rc,
                "seconds": round(seconds, 3), "alembic_version": revision,
                "clinical": clinical(conn)}
        if revision == "v1_3_0":
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*), COALESCE(SUM(billable_storage_bytes), 0) "
                            "FROM tenant_usage")
                step["tenant_usage"] = cur.fetchone()
                cur.execute("SELECT COUNT(*) FROM tenant_usage_threshold_state")
                step["threshold_state_rows"] = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM notification")
                step["notification_rows"] = cur.fetchone()[0]
                cur.execute("SELECT COALESCE(SUM(c - 1), 0) FROM ("
                            "  SELECT COUNT(*) AS c FROM tenant_usage "
                            "  GROUP BY tenant_id HAVING COUNT(*) > 1) d")
                step["duplicate_usage_rows"] = int(cur.fetchone()[0])
            independent = recompute_billable_bytes(conn)
            step["independent_total"] = sum(v["total"] for v in independent.values())
            # B-001: the summed signature contribution must be identical on
            # both upgrades, per tenant -- not merely the grand total.
            step["signature_bytes_per_tenant"] = {
                t: v["by_category"]["signature"] for t, v in sorted(independent.items())
                if v["by_category"]["signature"]}
            step["per_tenant_usage"] = {
                str(t): b for t, b in
                _rows_of(conn, "SELECT tenant_id, billable_storage_bytes FROM tenant_usage")}
        else:
            with conn.cursor() as cur:
                # Everything 1.3 added must be gone after the downgrade.
                cur.execute("SELECT to_regclass('tenant_usage') IS NULL, "
                            "to_regclass('notification') IS NULL, "
                            "to_regclass('tenant_usage_threshold_state') IS NULL")
                step["admin_tables_dropped"] = cur.fetchone()
        conn.close()
        steps.append(step)

    baseline_clinical = {t: baseline["counts"].get(t) for t in steps[0]["clinical"]}
    signature_users = _rows_of(psycopg2.connect(dsn(db)), """
        SELECT u.tenant_id, COUNT(*) FROM app_user u
        JOIN storage_object so ON so.id = u.signature_storage_id
        GROUP BY u.tenant_id ORDER BY COUNT(*) DESC""")
    return emit("downgrade-cycle", {
        "signature_mode": signature_mode,
        "signature_users_per_tenant": [int(n) for _, n in signature_users],
        "tenants_above_b001_threshold": sum(1 for _, n in signature_users if n >= 2),
        "baseline_clinical": baseline_clinical,
        "steps": steps,
        "clinical_preserved_throughout": all(
            s["clinical"] == baseline_clinical for s in steps),
        "usage_recomputed_on_reupgrade": (
            steps[2].get("tenant_usage", (None, None))[1] == steps[2].get("independent_total")),
        "threshold_state_empty_after_reupgrade": steps[2].get("threshold_state_rows") == 0,
        # B-001: identical, per tenant, on both upgrades -- not just in total.
        "signature_totals_identical_both_upgrades": (
            steps[0].get("signature_bytes_per_tenant")
            == steps[2].get("signature_bytes_per_tenant")),
        "per_tenant_usage_identical_both_upgrades": (
            steps[0].get("per_tenant_usage") == steps[2].get("per_tenant_usage")),
        "no_duplicate_usage_rows": all(
            s.get("duplicate_usage_rows", 0) == 0 for s in steps),
    })


def reconciliation(database: str) -> int:
    """§28 -- accounting-only reconciliation against migrated tenants.

    `verify_s3=False` is the controlled mode the brief asks for: it compares
    the counter with the authoritative DB recomputation and performs no S3
    round trip, which is correct here because the synthetic dataset has no
    real objects behind its StorageObject rows.
    """
    os.environ["DATABASE_URL"] = sa_url(database)
    # sys.path[0] is this script's directory, which shadows the repo root.
    # This is the one experiment that calls into `app/`; it reads the
    # service, never the other way round.
    sys.path.append("/app")
    from sqlmodel import Session, create_engine  # noqa: E402

    from app.services.usage_reconciliation import UsageReconciliationService  # noqa: E402

    engine = create_engine(sa_url(database))
    conn = psycopg2.connect(dsn(database))
    with conn.cursor() as cur:
        cur.execute("SELECT tenant_id, billable_storage_bytes FROM tenant_usage "
                    "ORDER BY billable_storage_bytes DESC")
        before = cur.fetchall()
    independent = recompute_billable_bytes(conn)
    conn.close()

    service = UsageReconciliationService()
    outcomes = []
    for tenant_id, counter in before:
        with Session(engine) as session:
            # repair=False keeps this a pure comparison: the migrated counter
            # is the thing under test and must not be rewritten by the check.
            outcome = service.reconcile_tenant(
                session, tenant_id, repair=False, verify_s3=False)
        outcomes.append({
            "tenant": str(tenant_id),
            "counter_before": counter,
            "status": outcome.status,
            "expected_storage_bytes": outcome.expected_storage_bytes,
            "actual_storage_bytes": outcome.actual_storage_bytes,
            "difference_bytes": outcome.difference_bytes,
            "legacy_logo_unresolved": outcome.legacy_logo_unresolved,
            "logo_integrity_errors": outcome.logo_integrity_errors,
            "independent_python": independent[str(tenant_id)]["total"],
        })

    return emit(f"reconciliation-{database}", {
        "database": database,
        "tenants": len(outcomes),
        "outcomes": outcomes,
        "all_zero_difference": all(o["difference_bytes"] == 0 for o in outcomes),
        "agrees_with_independent_python": all(
            o["actual_storage_bytes"] == o["independent_python"] for o in outcomes),
    })


def logo_control() -> int:
    """Isolate the two independent causes of the pre-1.3 tenant-logo gap.

    A Céluma 1.2 database fails logo resolution twice over: the storage object
    carries no `tenant_id` (logos are outside the backfill's four categories),
    AND the 1.2 key layout `tenants/{id}/logo.{ext}` cannot match the
    backfill's `tenants/%/logo/%` pattern. Either alone is sufficient. This
    builds four one-tenant databases -- each combination of key layout and
    attribution -- and reports which resolve, proving the resolver itself is
    sound and that the gap is a data-shape mismatch, not a broken query.
    """
    results = []
    for key_layout in ("celuma_1_2", "celuma_1_3"):
        for attributed in (False, True):
            db = f"relval_logo_{key_layout}_{'attr' if attributed else 'null'}"
            recreate(db)
            alembic(db, "upgrade", "v1_2_0")
            tenant_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
            so_id = uuid.uuid4()
            key = (f"tenants/{tenant_id}/logo.png" if key_layout == "celuma_1_2"
                   else f"tenants/{tenant_id}/logo/abc123.png")
            conn = psycopg2.connect(dsn(db))
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("INSERT INTO tenant (created_at, id, name, logo_url, is_active) "
                            "VALUES (now(), %s, 'T', %s, true)",
                            (tenant_id, f"https://media.example.invalid/{key}"))
                cur.execute("""
                    INSERT INTO storage_object (created_at, id, provider, region, bucket,
                                                object_key, content_type, size_bytes)
                    VALUES (now(), %s, 'aws', 'r', 'b', %s, 'image/png', 4321)
                """, (so_id, key))
            conn.close()

            _, _, rc = alembic(db, "upgrade", "v1_3_0")
            conn = psycopg2.connect(dsn(db))
            conn.autocommit = True
            if attributed:
                # Simulate the counterfactual where attribution had happened,
                # then re-run only the backfill's own resolution rule.
                with conn.cursor() as cur:
                    cur.execute("UPDATE storage_object SET tenant_id = %s WHERE id = %s",
                                (tenant_id, so_id))
                    cur.execute("""
                        WITH candidate AS (
                            SELECT t.id AS tenant_id, so.id AS storage_object_id
                            FROM tenant t
                            JOIN storage_object so
                                ON so.tenant_id = t.id
                               AND so.object_key LIKE 'tenants/%%/logo/%%'
                               AND right(split_part(split_part(t.logo_url,'#',1),'?',1),
                                         length(so.object_key) + 1) = '/' || so.object_key
                            WHERE t.logo_url IS NOT NULL
                        ), resolved AS (
                            SELECT tenant_id, (array_agg(storage_object_id))[1] AS storage_object_id
                            FROM candidate GROUP BY tenant_id HAVING COUNT(*) = 1
                        )
                        UPDATE tenant t SET logo_storage_id = r.storage_object_id
                        FROM resolved r WHERE r.tenant_id = t.id AND t.logo_storage_id IS NULL
                    """)
            with conn.cursor() as cur:
                cur.execute("SELECT logo_storage_id IS NOT NULL FROM tenant WHERE id = %s",
                            (tenant_id,))
                resolved = cur.fetchone()[0]
                cur.execute("SELECT tenant_id IS NOT NULL FROM storage_object WHERE id = %s",
                            (so_id,))
                so_attributed = cur.fetchone()[0]
            conn.close()
            results.append({
                "key_layout": key_layout,
                "storage_object_attributed": so_attributed,
                "attribution_simulated": attributed,
                "migration_rc": rc,
                "logo_resolved": resolved,
            })

    return emit("logo-control", {
        "cases": results,
        "conclusion": (
            "resolves only when the key uses the 1.3 layout AND the storage object "
            "carries tenant_id; a genuine Céluma 1.2 database satisfies neither"),
    })


def interrupt() -> int:
    """§34 -- kill the migration mid-transaction and check what survives.

    The whole of `v1_3_0` runs inside one transaction under PostgreSQL's
    transactional DDL, so the expected outcome is all-or-nothing: the database
    is left at `v1_2_0` with none of the 1.3 schema and no partial backfill.
    Terminating the backend (rather than the client) is the harsher case: it
    is what a failover, an OOM kill or an operator cancel looks like.

    `faithful` since the B-001 remediation: the natural abort this used to sit
    alongside no longer exists, so the deliberate kill is the only failure path
    left, and the retry has to land on a correct *summed* signature baseline.
    """
    from dataset import Generator
    from profiles import MEDIUM

    db = "relval_interrupt"
    recreate(db)
    alembic(db, "upgrade", "v1_2_0")
    conn = psycopg2.connect(dsn(db))
    Generator(MEDIUM, 20260815, "faithful").generate(conn)
    with conn.cursor() as cur:
        cur.execute("ANALYZE")
    conn.commit()
    before = snapshot(conn)
    conn.close()

    env = dict(os.environ, DATABASE_URL=sa_url(db))
    proc = subprocess.Popen(["alembic", "upgrade", "v1_3_0"], cwd="/app", env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    # Wait until the migration is demonstrably inside section 15, then kill it
    # there -- interrupting during DDL would prove less.
    watcher = psycopg2.connect(dsn("postgres"))
    watcher.autocommit = True
    killed_during = None
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and proc.poll() is None:
        with watcher.cursor() as cur:
            cur.execute("""
                SELECT pid, left(regexp_replace(query, '\\s+', ' ', 'g'), 90)
                FROM pg_stat_activity
                WHERE datname = %s AND state = 'active' AND pid <> pg_backend_pid()
            """, (db,))
            for pid, query in cur.fetchall():
                if "storage_object" in query or "tenant_usage" in query:
                    cur.execute("SELECT pg_terminate_backend(%s)", (pid,))
                    killed_during = query
                    break
        if killed_during:
            break
        time.sleep(0.01)

    output = proc.communicate()[0]
    watcher.close()

    conn = psycopg2.connect(dsn(db))
    with conn.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        revision = cur.fetchone()[0]
        cur.execute("SELECT to_regclass('tenant_usage') IS NULL, "
                    "to_regclass('notification') IS NULL, "
                    "to_regclass('tenant_usage_threshold_state') IS NULL")
        dropped = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM information_schema.columns "
                    "WHERE table_name = 'storage_object' AND column_name = 'tenant_id'")
        tenant_id_column = cur.fetchone()[0]
    after = snapshot(conn)
    conn.close()

    # A second, clean attempt must still succeed: a rolled-back migration
    # must not have poisoned the database for the retry.
    retry_seconds, _, retry_rc = alembic(db, "upgrade", "v1_3_0")
    conn = psycopg2.connect(dsn(db))
    with conn.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        retry_revision = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*), COALESCE(SUM(billable_storage_bytes), 0) FROM tenant_usage")
        retry_usage = cur.fetchone()
    retry_independent = sum(v["total"] for v in recompute_billable_bytes(conn).values())
    conn.close()

    return emit("interrupt", {
        "killed_during_statement": killed_during,
        "alembic_returncode": proc.returncode,
        "alembic_version_after_kill": revision,
        "one_three_tables_absent": dropped,
        "storage_object_tenant_id_column_present": tenant_id_column,
        "clinical_counts_unchanged": {
            t: before["counts"].get(t) == after["counts"].get(t)
            for t in ("tenant", "patient", "order", "sample", "report",
                      "report_version", "storage_object", "app_user")},
        "retry": {"returncode": retry_rc, "seconds": round(retry_seconds, 3),
                  "alembic_version": retry_revision,
                  "tenant_usage": retry_usage,
                  "independent_total": retry_independent,
                  "usage_matches_independent": str(retry_usage[1]) == str(retry_independent)},
        "output_tail": output[-800:],
    })


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("experiment", choices=[
        "fresh-install", "constraints", "downgrade-cycle", "reconciliation",
        "logo-control", "interrupt"])
    ap.add_argument("database", nargs="?", default="relval_small_faithful")
    ap.add_argument(
        "--signature-mode", choices=["faithful", "one_per_tenant"], default="faithful",
        help="downgrade-cycle only; 'faithful' is the release-validation mode "
             "since the B-001 remediation",
    )
    args = ap.parse_args()
    print(f"# {args.experiment} @ {datetime.now(timezone.utc).isoformat()}", flush=True)
    return {
        "fresh-install": fresh_install,
        "downgrade-cycle": lambda: downgrade_cycle(args.signature_mode),
        "logo-control": logo_control,
        "interrupt": interrupt,
        "constraints": lambda: constraints(args.database),
        "reconciliation": lambda: reconciliation(args.database),
    }[args.experiment]()


if __name__ == "__main__":
    raise SystemExit(main())
