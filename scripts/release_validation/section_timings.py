"""Attribute migration wall-clock time to sections of the frozen migration (§14).

TEST TOOLING ONLY. Never imported from `app/`.

The brief forbids modifying `v1_3_0` to add instrumentation, so the timing
comes from the server instead: `run_profile.py` sets
`log_min_duration_statement = 0` on the validation database for the duration
of the upgrade, and PostgreSQL logs every statement with its duration. This
script reads that log back and matches statements against the migration's own
SQL text.

    docker logs celuma-relval-db > pg.log
    python scripts/release_validation/section_timings.py pg.log --pid 354

`--pid` restricts the parse to one backend, which is how a single migration
run is isolated from the generation and verification traffic around it.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

#: Fragments identifying each measurable operation. First match wins, so the
#: order is by specificity, NOT by execution order: the eight-CTE baseline
#: INSERT contains the text of several backfills (it aggregates over the same
#: tables), and would otherwise be attributed to whichever backfill pattern
#: was tested first.
SECTIONS = [
    ("15b_tenant_usage_baseline", r"INSERT INTO tenant_usage"),
    ("15a1_backfill_sample_images",
     r"UPDATE storage_object so\s+SET tenant_id = si\.tenant_id\s+FROM sample_image si"),
    ("15a2_backfill_renditions",
     r"UPDATE storage_object so\s+SET tenant_id = si\.tenant_id\s+FROM sample_image_rendition sir"),
    ("15a3_backfill_report_json_and_legacy_pdf",
     r"SET tenant_id = r\.tenant_id\s+FROM report_version rv"),
    ("15a4_backfill_signatures", r"SET tenant_id = u\.tenant_id\s+FROM app_user u"),
    ("15c_logo_backfill_update", r"UPDATE tenant t\s+SET logo_storage_id"),
    ("15c_logo_backfill_report", r"SELECT\s+\(SELECT COUNT\(\*\) FROM tenant WHERE logo_url"),
    ("ddl_add_storage_tenant_id", r"ALTER TABLE storage_object ADD COLUMN tenant_id"),
    ("ddl_index_storage_tenant_id", r"CREATE INDEX ix_storage_object_tenant_id"),
    ("ddl_fk_storage_tenant_id", r"ALTER TABLE storage_object ADD CONSTRAINT storage_object_tenant_id_fkey"),
]

LINE = re.compile(
    r"^(?P<ts>\S+ \S+ \S+) \[(?P<pid>\d+)\] LOG:\s+duration: (?P<ms>[\d.]+) ms\s+"
    r"(?:statement|execute [^:]*):(?P<sql>.*)$"
)


def parse(path: Path, pid: int | None) -> dict:
    """Group logged statement durations by migration section.

    Statements span multiple log lines; a line that does not start a new log
    record is a continuation of the previous statement.
    """
    records: list[tuple[int, float, str]] = []
    current: list[str] | None = None
    cur_pid = cur_ms = None

    for raw in path.read_text(errors="replace").splitlines():
        m = LINE.match(raw)
        if m:
            if current is not None:
                records.append((cur_pid, cur_ms, "\n".join(current)))
            cur_pid = int(m.group("pid"))
            cur_ms = float(m.group("ms"))
            current = [m.group("sql")]
        elif current is not None and not re.match(r"^\S+ \S+ \S+ \[\d+\] ", raw):
            current.append(raw)
        else:
            if current is not None:
                records.append((cur_pid, cur_ms, "\n".join(current)))
            current = None

    if current is not None:
        records.append((cur_pid, cur_ms, "\n".join(current)))

    totals: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    unmatched_ms = 0.0
    for rec_pid, ms, sql in records:
        if pid is not None and rec_pid != pid:
            continue
        for name, pattern in SECTIONS:
            if re.search(pattern, sql, re.IGNORECASE):
                totals[name] += ms / 1000.0
                counts[name] += 1
                break
        else:
            unmatched_ms += ms

    return {
        "sections": {k: round(v, 3) for k, v in sorted(totals.items())},
        "statement_counts": dict(counts),
        "matched_total_seconds": round(sum(totals.values()), 3),
        "unmatched_total_seconds": round(unmatched_ms / 1000.0, 3),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("logfile", type=Path)
    ap.add_argument("--pid", type=int, default=None)
    args = ap.parse_args()
    json.dump(parse(args.logfile, args.pid), sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
