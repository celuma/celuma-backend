#!/usr/bin/env python3
"""Measure list endpoints for every synthetic tenant built by `dataset.py`.

TEST TOOLING ONLY (Céluma 1.4, issues #5/#6). Runs the real FastAPI app
in-process (TestClient, no lifespan, so no background worker starts) against
the disposable `celuma_listperf*` database and records, per request:

* `wall_ms`  — request handling inside the process: routing, auth, database,
  ORM hydration, Pydantic validation and JSON encoding. No network.
* `sql_count` / `sql_ms` — statements executed and time spent inside
  `cursor.execute` (database work plus the container-to-container round trip
  and result transfer). `app_ms` is `wall_ms - sql_ms`.
* `count_ms` — time of the paged total (`SELECT count(*) …`) alone.
* `bytes` / `gzip_bytes` — response body size, raw and gzip-6, to bound the
  network cost without assuming whether a proxy compresses API responses.

Scenarios that pass paging/search/filter/sort parameters still run against an
API that ignores them; `items` then shows the whole collection was returned.

Usage (inside the API container):

    python scripts/list_performance/benchmark.py --database celuma_listperf \
        --runs 5 --label baseline > report.json
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

DB_PREFIX = "celuma_listperf"


def _configure(database: str) -> None:
    from sqlalchemy.engine.url import make_url

    url = make_url(os.environ["DATABASE_URL"]).set(database=database)
    if not database.startswith(DB_PREFIX) or url.host not in {"db", "localhost", "127.0.0.1"}:
        raise SystemExit(f"refusing to benchmark {url.host}/{database}")
    os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
    os.environ["EMAIL_ENABLED"] = "false"
    os.environ["USAGE_RECONCILIATION_ENABLED"] = "false"


#: (scenario, path, params). `{patient_id}`/`{physician_id}` resolve to the
#: tenant's busiest patient/physician so nested lists are worst-case.
SCENARIOS = [
    ("samples:initial", "/api/v1/laboratory/samples/", {}),
    ("orders:initial", "/api/v1/laboratory/orders/", {}),
    ("reports:initial", "/api/v1/reports/", {}),
    ("patients:initial", "/api/v1/patients/", {}),
    ("physicians:initial", "/api/v1/requesting-physicians/", {"active_only": "false"}),
    ("patient_orders:initial", "/api/v1/laboratory/patients/{patient_id}/orders", {}),
    ("physician_orders:initial", "/api/v1/requesting-physicians/{physician_id}/orders", {}),
    ("worklist:initial", "/api/v1/me/worklist", {"page": "1", "page_size": "20"}),
    # Paged contract (Céluma 1.4). Ignored by an API that predates it.
    ("samples:page1", "/api/v1/laboratory/samples/", {"page": "1", "page_size": "20"}),
    ("samples:page_last", "/api/v1/laboratory/samples/", {"page": "{samples_last_page}", "page_size": "20"}),
    ("samples:search_code", "/api/v1/laboratory/samples/", {"page": "1", "page_size": "20", "q": "CU276262"}),
    ("samples:search_digits", "/api/v1/laboratory/samples/", {"page": "1", "page_size": "20", "q": "276262"}),
    ("samples:search_name", "/api/v1/laboratory/samples/", {"page": "1", "page_size": "20", "q": "munoz"}),
    ("samples:search_typo", "/api/v1/laboratory/samples/", {"page": "1", "page_size": "20", "q": "biosia"}),
    ("samples:filters", "/api/v1/laboratory/samples/",
     {"page": "1", "page_size": "20", "state": "RECEIVED,PROCESSING", "sample_type_id": "none"}),
    ("samples:sort_patient_desc", "/api/v1/laboratory/samples/",
     {"page": "1", "page_size": "20", "sort": "patient", "order": "desc"}),
    ("samples:combined_page2", "/api/v1/laboratory/samples/",
     {"page": "2", "page_size": "20", "q": "munoz", "state": "RECEIVED,PROCESSING", "sort": "order_code",
      "order": "desc"}),
    ("samples:filter_options", "/api/v1/laboratory/samples/filter-options", {}),
    ("orders:page1", "/api/v1/laboratory/orders/", {"page": "1", "page_size": "20"}),
    ("orders:search_name", "/api/v1/laboratory/orders/", {"page": "1", "page_size": "20", "q": "munoz"}),
    ("orders:filters", "/api/v1/laboratory/orders/",
     {"page": "1", "page_size": "20", "status": "RECEIVED,PROCESSING", "has_report": "false"}),
    ("orders:combined_page2", "/api/v1/laboratory/orders/",
     {"page": "2", "page_size": "20", "q": "dr", "status": "RECEIVED,PROCESSING,DIAGNOSIS", "sort": "requester"}),
    ("orders:filter_options", "/api/v1/laboratory/orders/filter-options", {}),
    ("reports:page1", "/api/v1/reports/", {"page": "1", "page_size": "20"}),
    ("reports:search_code", "/api/v1/reports/", {"page": "1", "page_size": "20", "q": "CU-1"}),
    ("reports:filter_options", "/api/v1/reports/filter-options", {}),
]

_PARAM_RE = re.compile(r"('[^']*'|\b\d+\b)")


def _template(statement: str) -> str:
    return " ".join(_PARAM_RE.sub("?", statement).split())[:160]


class SqlMeter:
    def __init__(self, engine) -> None:
        from sqlalchemy import event

        self.count = 0
        self.seconds = 0.0
        self.count_seconds = 0.0
        self.templates: Counter = Counter()
        self._start: list[float] = []
        event.listen(engine, "before_cursor_execute", self._before)
        event.listen(engine, "after_cursor_execute", self._after)

    def reset(self) -> None:
        self.count, self.seconds, self.count_seconds, self.templates = 0, 0.0, 0.0, Counter()

    def _before(self, conn, cursor, statement, parameters, context, executemany):
        self._start.append(time.perf_counter())

    def _after(self, conn, cursor, statement, parameters, context, executemany):
        elapsed = time.perf_counter() - self._start.pop()
        self.seconds += elapsed
        if statement.lstrip().upper().startswith("SELECT COUNT(*)"):
            self.count_seconds += elapsed
        self.count += 1
        self.templates[_template(statement)] += 1


def _tenants(session):
    from sqlalchemy import text

    rows = session.exec(text("""
        SELECT t.name, t.id, u.id FROM tenant t
        JOIN app_user u ON u.tenant_id = t.id AND u.username = replace(t.name, 'Listperf ', '') || '0'
        WHERE t.name LIKE 'Listperf %' ORDER BY t.name
    """)).all()
    out = []
    for name, tenant_id, user_id in rows:
        patient_id = session.exec(text("""SELECT patient_id FROM "order" WHERE tenant_id = :t AND patient_id IS NOT NULL
            GROUP BY patient_id ORDER BY count(*) DESC, patient_id LIMIT 1"""), params={"t": tenant_id}).first()[0]
        physician_id = session.exec(text("""SELECT requesting_physician_id FROM "order" WHERE tenant_id = :t
            AND requesting_physician_id IS NOT NULL GROUP BY requesting_physician_id
            ORDER BY count(*) DESC, requesting_physician_id LIMIT 1"""), params={"t": tenant_id}).first()[0]
        samples = session.exec(text("SELECT count(*) FROM sample WHERE tenant_id = :t"), params={"t": tenant_id}).first()[0]
        out.append({"name": name.replace("Listperf ", ""), "tenant_id": str(tenant_id), "user_id": str(user_id),
                    "patient_id": str(patient_id), "physician_id": str(physician_id),
                    "samples_last_page": str(max(1, -(-samples // 20)))})
    return out


def _items(body) -> int | None:
    if isinstance(body, list):
        return len(body)
    if isinstance(body, dict):
        for key in ("samples", "orders", "reports", "patients", "items"):
            if isinstance(body.get(key), list):
                return len(body[key])
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", default=DB_PREFIX)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--label", default="run")
    parser.add_argument("--only", default="", help="comma-separated scenario prefixes")
    parser.add_argument("--tenants", default="", help="comma-separated tenant profile names")
    args = parser.parse_args()
    _configure(args.database)

    import logging

    logging.disable(logging.CRITICAL)
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from app.core.db import engine
    from app.core.security import create_jwt
    from app.main import app, rate_limit_storage

    meter = SqlMeter(engine)
    client = TestClient(app)
    with Session(engine) as session:
        tenants = _tenants(session)
    if args.tenants:
        tenants = [t for t in tenants if t["name"] in args.tenants.split(",")]
    prefixes = tuple(p for p in args.only.split(",") if p)

    results = []
    for tenant in tenants:
        headers = {"Authorization": f"Bearer {create_jwt(sub=tenant['user_id'])}"}
        for name, path, params in SCENARIOS:
            if prefixes and not name.startswith(prefixes):
                continue
            url = path.format(**tenant)
            query = {k: v.format(**tenant) for k, v in params.items()}
            runs = []
            for i in range(args.warmup + args.runs):
                rate_limit_storage.clear()
                meter.reset()
                started = time.perf_counter()
                response = client.get(url, params=query, headers=headers)
                wall = (time.perf_counter() - started) * 1000
                if i < args.warmup:
                    continue
                body = response.json() if response.headers.get("content-type", "").startswith("application/json") else None
                runs.append({"status": response.status_code, "wall_ms": wall, "sql_count": meter.count,
                             "sql_ms": meter.seconds * 1000, "count_ms": meter.count_seconds * 1000,
                             "bytes": len(response.content),
                             "gzip_bytes": len(gzip.compress(response.content, 6)), "items": _items(body),
                             "total": body.get("total") if isinstance(body, dict) else None,
                             "top_sql": meter.templates.most_common(3)})
            walls = [r["wall_ms"] for r in runs]
            sqls = [r["sql_ms"] for r in runs]
            last = runs[-1]
            summary = {
                "label": args.label, "tenant": tenant["name"], "scenario": name, "status": last["status"],
                "runs": len(runs), "wall_ms_median": round(statistics.median(walls), 1),
                "wall_ms_min": round(min(walls), 1), "wall_ms_max": round(max(walls), 1),
                "sql_ms_median": round(statistics.median(sqls), 1),
                "count_ms_median": round(statistics.median(r["count_ms"] for r in runs), 1),
                "app_ms_median": round(statistics.median(w - s for w, s in zip(walls, sqls)), 1),
                "sql_count": last["sql_count"], "bytes": last["bytes"], "gzip_bytes": last["gzip_bytes"],
                "items": last["items"], "total": last["total"], "top_sql": last["top_sql"],
            }
            results.append(summary)
            print(f"{tenant['name']:>6} {name:<28} {summary['status']} wall={summary['wall_ms_median']:>9}ms "
                  f"sql={summary['sql_ms_median']:>9}ms count={summary['count_ms_median']:>6}ms "
                  f"n_sql={summary['sql_count']:>6} "
                  f"bytes={summary['bytes']:>9} gz={summary['gzip_bytes']:>8} items={summary['items']} "
                  f"total={summary['total']}", file=sys.stderr, flush=True)
    json.dump({"label": args.label, "database": args.database, "runs": args.runs, "warmup": args.warmup,
               "results": results}, sys.stdout, indent=1, default=str)
    print()


if __name__ == "__main__":
    main()
