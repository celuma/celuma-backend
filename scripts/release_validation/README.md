# Release validation tooling

**Test tooling only.** Nothing here is imported from `app/`, nothing runs at
application startup, and no release functionality depends on it. Built for
Céluma 1.3 Phase 5 Block B — Database Upgrade & Data Integrity Validation.

It never contacts AWS, never creates an S3 object, and contains no PHI: every
name, code and address it generates comes from a seeded PRNG over fixed
nonsense-syllable tables.

## What it does

Builds a deterministic, production-shaped, entirely synthetic Céluma **1.2**
database; migrates it with the frozen `v1_3_0`; and verifies the result —
clinical preservation, tenant isolation, storage attribution, usage baseline,
notification safety, locking, timing and repeatability.

## Files

| File | Purpose |
| --- | --- |
| `profiles.py` | SMALL / MEDIUM / LARGE dataset shapes |
| `dataset.py` | The generator. Faithful to Céluma 1.2 column and object-key layouts |
| `snapshot.py` | Integrity fingerprints, plus an **independent** re-implementation of the billable-usage contract |
| `run_profile.py` | End-to-end: build → snapshot → migrate (timed) → verify → JSON report |
| `concurrency_probe.py` | Lock observation and a synthetic concurrent 1.2 writer |
| `mixed_version_writes.py` | 1.2-shaped writes against the migrated 1.3 schema |
| `bounded_experiments.py` | Fresh install, notification constraints, downgrade cycle, reconciliation, tenant-logo control, deliberate interrupt |
| `section_timings.py` | Per-section migration timing, parsed from the PostgreSQL log |

## Setup

An **isolated** PostgreSQL container. Never point this at the developer's
`celumadb`.

```bash
docker volume create celuma-relval-pgdata
docker run -d --name celuma-relval-db --network celuma-backend_default \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=postgres \
  -v celuma-relval-pgdata:/var/lib/postgresql/data -p 5433:5432 \
  postgres:16.13 \
  -c log_min_duration_statement=200 -c log_lock_waits=on -c track_io_timing=on
```

## Usage

```bash
# One profile, end to end -- `faithful` is the default and the release mode
docker compose exec api python scripts/release_validation/run_profile.py MEDIUM \
    --seed 20260815 --signature-mode faithful --suffix _faithful

# Reproduce the historical B-001 abort (only against the superseded blob ff83b6b)
docker compose exec api python scripts/release_validation/run_profile.py SMALL \
    --seed 20260815 --signature-mode faithful --expect-failure

# Bounded experiments
docker compose exec api python scripts/release_validation/bounded_experiments.py fresh-install
docker compose exec api python scripts/release_validation/bounded_experiments.py logo-control
docker compose exec api python scripts/release_validation/bounded_experiments.py downgrade-cycle --signature-mode faithful
docker compose exec api python scripts/release_validation/bounded_experiments.py interrupt
docker compose exec api python scripts/release_validation/bounded_experiments.py constraints relval_small_faithful
docker compose exec api python scripts/release_validation/bounded_experiments.py reconciliation relval_medium_faithful

# Locking: prepare, then migrate under observation
docker compose exec api python scripts/release_validation/run_profile.py LARGE \
    --seed 20260815 --suffix _lock --prepare-only
docker compose exec api python scripts/release_validation/concurrency_probe.py \
    relval_large_lock --seconds 32 --observe-only &
docker compose exec api python scripts/release_validation/run_profile.py LARGE \
    --seed 20260815 --suffix _lock --resume
```

Reports are written to `/app/.release-validation/` (git-ignored).

## Two things to know before running

**`faithful` is the release-validation dataset.** It gives 40% of users a
signature, so tenants routinely hold several — what a real laboratory looks
like.

Against the superseded migration blob `ff83b6b` this mode **aborted** the
migration: finding **B-001**, where the `tenant_usage` baseline's `signature`
CTE lacked `SUM`/`GROUP BY` and any tenant with two or more signature-bearing
users violated `tenant_usage_pkey`. `one_per_tenant` was the bounded workaround
that let everything else be measured in the meantime.

B-001 was fixed and `v1_3_0` re-frozen at blob `9f7fb3c`, so `faithful` now
completes at every scale and is the basis of all release evidence.
`one_per_tenant` is retained as a historical/debug mode only — do not present
its results as realistic.

**LARGE needs disk.** Roughly 390 MB before the migration and 624 MB after,
inside the Docker VM's filesystem. Two LARGE databases at once exhausted a
7.8 GB VM disk and crashed PostgreSQL. Drop each one before building the next:

```bash
docker exec celuma-relval-db psql -U postgres -c "DROP DATABASE IF EXISTS relval_large;"
```

## Full documentation

`docs/celuma-1.3/phase-5-block-b/` — in particular
`block-b-dataset-contract.md` for the dataset's fidelity rules and reproduction
steps, and `block-b-release-findings.md` for what it found.
