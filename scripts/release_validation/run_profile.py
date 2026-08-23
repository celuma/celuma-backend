"""Run one profile end to end: build a 1.2 database, migrate it, verify it.

TEST TOOLING ONLY. Never imported from `app/`.

    python scripts/release_validation/run_profile.py SMALL --seed 20260815

Sequence, matching §13 of the Block B brief:

    fresh database -> alembic upgrade v1_2_0 -> generate synthetic data
      -> ANALYZE -> pre-migration snapshot
      -> alembic upgrade v1_3_0  (wall-clock measured)
      -> post-migration snapshot + verification -> JSON report

Per-section timing (§14) is obtained WITHOUT modifying the frozen migration:
`log_min_duration_statement` is set to 0 on the validation database, and the
server log is read back afterwards and matched against the migration's own
statement text. The migration file is never touched, read for timing, or
wrapped.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg2  # noqa: E402

from dataset import Generator  # noqa: E402
from profiles import PROFILES  # noqa: E402
from snapshot import snapshot, verify  # noqa: E402

DB_HOST = os.environ.get("RELVAL_DB_HOST", "celuma-relval-db")
DB_PORT = os.environ.get("RELVAL_DB_PORT", "5432")
DB_USER = os.environ.get("RELVAL_DB_USER", "postgres")
DB_PASS = os.environ.get("RELVAL_DB_PASS", "postgres")

OUT_DIR = Path(os.environ.get("RELVAL_OUT", "/app/.release-validation"))

#: Fragments identifying each measurable section of the frozen migration's
#: section 15, matched against statements the server logged.
SECTION_PATTERNS = {
    "backfill_sample_images": r"UPDATE storage_object so\s+SET tenant_id = si\.tenant_id\s+FROM sample_image si",
    "backfill_renditions": r"FROM sample_image_rendition sir",
    "backfill_report_json_and_legacy_pdf": r"FROM report_version rv\s+JOIN report r",
    "backfill_signatures": r"FROM app_user u\s+WHERE u\.signature_storage_id",
    "tenant_usage_baseline": r"INSERT INTO tenant_usage",
    "logo_backfill": r"UPDATE tenant t\s+SET logo_storage_id",
}


def dsn(database: str) -> str:
    return f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{database}"


def sqlalchemy_url(database: str) -> str:
    return f"postgresql+psycopg2://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{database}"


def connect(database: str):
    conn = psycopg2.connect(dsn(database))
    conn.autocommit = False
    return conn


def recreate_database(name: str) -> None:
    conn = psycopg2.connect(dsn("postgres"))
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
        cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        cur.execute(f'CREATE DATABASE "{name}"')
    conn.close()


def alembic(database: str, target: str) -> tuple[float, str]:
    """Run `alembic upgrade|downgrade <target>`, returning (seconds, output)."""
    env = dict(os.environ, DATABASE_URL=sqlalchemy_url(database))
    started = time.monotonic()
    proc = subprocess.run(
        ["alembic", "upgrade" if target != "downgrade" else "downgrade", target],
        cwd="/app", env=env, capture_output=True, text=True,
    )
    elapsed = time.monotonic() - started
    output = proc.stdout + proc.stderr
    if proc.returncode != 0:
        raise SystemExit(f"alembic {target} failed ({proc.returncode}):\n{output}")
    return elapsed, output


def set_statement_logging(database: str, enabled: bool) -> None:
    """Turn full statement-duration logging on or off for one database.

    Database-scoped so the migration's own connection picks it up without any
    change to the migration, and without logging every other database on the
    server.
    """
    conn = psycopg2.connect(dsn("postgres"))
    conn.autocommit = True
    with conn.cursor() as cur:
        value = "0" if enabled else "200"
        cur.execute(f'ALTER DATABASE "{database}" SET log_min_duration_statement = {value}')
    conn.close()


def parse_section_timings(log_text: str) -> dict[str, float]:
    """Extract per-statement durations for section 15 from the server log."""
    timings: dict[str, float] = {}
    # `duration: 1234.567 ms  statement: <sql>` -- the SQL may span lines, so
    # split on the duration marker and inspect each following block.
    blocks = re.split(r"duration:\s+([\d.]+)\s+ms\s+statement:", log_text)
    for i in range(1, len(blocks) - 1, 2):
        ms = float(blocks[i])
        body = blocks[i + 1]
        for name, pattern in SECTION_PATTERNS.items():
            if re.search(pattern, body, re.IGNORECASE):
                timings[name] = timings.get(name, 0.0) + ms / 1000.0
                break
    return timings


def db_stats(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_database_size(current_database())")
        size = cur.fetchone()[0]
        cur.execute("""
            SELECT relname, pg_total_relation_size(c.oid)
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r'
            ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 20""")
        tables = {r[0]: r[1] for r in cur.fetchall()}
    return {"database_bytes": size, "largest_tables": tables}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("profile", choices=sorted(PROFILES))
    ap.add_argument("--seed", type=int, default=20260815)
    ap.add_argument("--suffix", default="", help="distinguish repeat runs of one profile")
    ap.add_argument("--keep", action="store_true", help="do not drop the database first")
    ap.add_argument(
        "--signature-mode", choices=["faithful", "one_per_tenant"], default="faithful",
        help="'faithful' reproduces a real lab and triggers B-001; 'one_per_tenant' "
             "is the bounded workaround that lets the rest of Block B be measured",
    )
    ap.add_argument(
        "--expect-failure", action="store_true",
        help="record the migration failure as the result instead of aborting the run",
    )
    # Split the run in two so the concurrency probe (§15) can be attached to a
    # prepared database without its polling appearing in the clean timing runs.
    ap.add_argument("--prepare-only", action="store_true",
                    help="build and snapshot the 1.2 database, then stop")
    ap.add_argument("--resume", action="store_true",
                    help="migrate and verify a database left by --prepare-only")
    args = ap.parse_args()

    profile = PROFILES[args.profile]
    db_name = f"relval_{args.profile.lower()}{args.suffix}"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report: dict = {
        "profile": args.profile,
        "seed": args.seed,
        "database": db_name,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    prepared_path = OUT_DIR / f"{args.profile.lower()}{args.suffix}-prepared.json"
    if args.resume:
        report = json.loads(prepared_path.read_text())
        print(f"[{args.profile}] resuming prepared database {db_name}", flush=True)
    else:
        print(f"[{args.profile}] recreating {db_name}", flush=True)
        if not args.keep:
            recreate_database(db_name)

        print(f"[{args.profile}] alembic upgrade v1_2_0", flush=True)
        seconds, _ = alembic(db_name, "v1_2_0")
        report["upgrade_to_v1_2_0_seconds"] = round(seconds, 3)

        print(f"[{args.profile}] generating synthetic 1.2 dataset "
              f"(expected ~{profile.estimated_storage_objects():,} storage objects)",
              flush=True)
        conn = connect(db_name)
        started = time.monotonic()
        result = Generator(profile, args.seed, args.signature_mode).generate(conn)
        report["generation_seconds"] = round(time.monotonic() - started, 3)
        report["dataset"] = {
            "rows": result.rows,
            "storage_categories": result.storage_categories,
            "notes": result.tenant_notes,
        }
        print(f"[{args.profile}] generated: {json.dumps(result.rows)}", flush=True)

        with conn.cursor() as cur:
            cur.execute("ANALYZE")
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            report["postgresql_version"] = cur.fetchone()[0]

        report["pre_migration"] = snapshot(conn)
        report["pre_migration_db_stats"] = db_stats(conn)
        conn.close()

        if args.prepare_only:
            prepared_path.write_text(json.dumps(report, indent=2, default=str))
            print(f"[{args.profile}] prepared at v1_2_0; state in {prepared_path}", flush=True)
            return 0

    # -- the measured transition -------------------------------------------
    set_statement_logging(db_name, True)
    log_marker = datetime.now(timezone.utc).isoformat()
    print(f"[{args.profile}] alembic upgrade v1_3_0 ...", flush=True)
    started_wall = datetime.now(timezone.utc)
    failure = None
    try:
        seconds, output = alembic(db_name, "v1_3_0")
    except SystemExit as exc:
        if not args.expect_failure:
            set_statement_logging(db_name, False)
            raise
        seconds, output, failure = 0.0, str(exc), "migration aborted"
    finished_wall = datetime.now(timezone.utc)
    set_statement_logging(db_name, False)

    report["migration"] = {
        "start": started_wall.isoformat(),
        "end": finished_wall.isoformat(),
        "wall_clock_seconds": round(seconds, 3),
        "log_marker": log_marker,
        "failure": failure,
        "alembic_output": output[-8000:],
    }

    if failure:
        # The rollback state is the finding. Record what the database is
        # actually left at and stop -- there is nothing after v1_3_0 to verify.
        conn = connect(db_name)
        with conn.cursor() as cur:
            cur.execute("SELECT version_num FROM alembic_version")
            report["alembic_version_after"] = cur.fetchone()[0]
            cur.execute("SELECT to_regclass('tenant_usage') IS NOT NULL, "
                        "to_regclass('notification') IS NOT NULL")
            left_over = cur.fetchone()
        report["rollback_left_1_3_artifacts"] = {
            "tenant_usage": left_over[0], "notification": left_over[1]}
        conn.close()
        out_path = OUT_DIR / f"{args.profile.lower()}{args.suffix}-report.json"
        out_path.write_text(json.dumps(report, indent=2, default=str))
        print(f"[{args.profile}] MIGRATION FAILED; database left at "
              f"{report['alembic_version_after']}; report at {out_path}", flush=True)
        return 0

    print(f"[{args.profile}] migration wall clock: {seconds:.3f}s", flush=True)

    conn = connect(db_name)
    report["verification"] = verify(conn, report["pre_migration"])
    report["post_migration_db_stats"] = db_stats(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        report["alembic_version_after"] = cur.fetchone()[0]
    conn.close()

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    out_path = OUT_DIR / f"{args.profile.lower()}{args.suffix}-report.json"
    out_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"[{args.profile}] report written to {out_path}", flush=True)

    v = report["verification"]
    print(json.dumps({
        "wall_clock_seconds": report["migration"]["wall_clock_seconds"],
        "storage_objects": report["pre_migration"]["counts"]["storage_object"],
        "rows_lost": v["rows_lost"],
        "cross_tenant_mismatches": v["cross_tenant_mismatches"],
        "usage_missing": v["usage"]["missing_usage_rows"],
        "usage_mismatches": v["usage"]["mismatching_tenants"],
        "side_effects": v["migration_side_effects"],
        "logo": v["logo"],
    }, indent=2, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
