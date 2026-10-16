# List performance benchmark

**Test tooling only** (Céluma 1.4, issues #5/#6/#7). Nothing here is imported
from `app/` or runs at application startup. It never contacts AWS, sends
email or reads a shared database: both scripts refuse any database whose name
does not start with `celuma_listperf` or whose host is not a local one.

The contract it measures is
`celuma-engineering/specs/lists/search-and-pagination.md`; the recorded
baseline, results and targets live there too.

## Files

| File | Purpose |
| --- | --- |
| `dataset.py` | Deterministic synthetic tenants (`small`, `medium`, `large`, optional `--filler` tenants) **in one database**, with identifier neighbours (`CU276262`/`CU276263`/`BX276262`…), COD-01-shaped codes (`CU-011026`) and accented surnames as search probes. No PHI. |
| `benchmark.py` | Runs the real FastAPI app in-process (no lifespan, no workers) and records per request: wall time, SQL statement count and time, the `count(*)` share, and raw/gzip payload size. Median of N runs after warm-up. |

## Usage

All commands run inside the API image so `db` resolves. Pass an explicit
`DATABASE_URL` for the disposable database and blank AWS credentials so the
developer's `.env` cannot reach real storage.

```bash
# 1. Build the dataset (drops, recreates and migrates the database first).
docker compose run --rm --no-deps -T \
  -e DATABASE_URL=postgresql+psycopg2://postgres:postgres@db:5432/celuma_listperf \
  -e AWS_ACCESS_KEY_ID= -e AWS_SECRET_ACCESS_KEY= \
  api python scripts/list_performance/dataset.py --database celuma_listperf --reset > dataset.json

# Optional: a shared database where other laboratories grow the tables
# (18 tenants, ~185k samples with --filler 15).
docker compose run --rm --no-deps -T -e DATABASE_URL=… api \
  python scripts/list_performance/dataset.py --database celuma_listperf_shared --reset --filler 15

# 2. Measure (5 runs after 1 warm-up; --only/--tenants narrow the run).
docker compose run --rm --no-deps -T -e DATABASE_URL=… -e AWS_ACCESS_KEY_ID= -e AWS_SECRET_ACCESS_KEY= \
  api python scripts/list_performance/benchmark.py --database celuma_listperf --runs 5 --label after > after.json
```

The summary line per scenario goes to stderr, the full JSON report to
stdout. Keep reports out of the repository.

To measure the code *before* a change, extract that revision
(`git archive <rev> | tar -x -C <dir>`), copy `benchmark.py` into it and run
the same command in a container that mounts that directory as `/app`
instead of this checkout. A database migrated by newer code may carry
indexes the older code did not have; build the "before" database with the
older revision's migrations when comparing.

## Reading the numbers

`wall_ms` is server-side work only (routing, auth, SQL, ORM, Pydantic, JSON);
it excludes the network. `sql_ms` includes the container-to-container round
trip, which is far shorter than an ECS-to-RDS hop, so per-statement overhead
in a deployed environment is larger than measured here. Results come from a
developer machine (Docker VM: 2 vCPU, 2 GB; PostgreSQL 15) and are comparable
with each other, not with production.

Never run two benchmarks, or a benchmark and the pytest suite, at the same
time: they share the database server and distort each other's timings.
