"""v1.4.0 - List search extension and list indexes (Céluma 1.4 release migration, in development)

Revision ID: v1_4_0
Revises: v1_3_1
Create Date: 2026-10-14

The database revision for the Céluma 1.4 release, following the standing
contract of one Alembic revision per shipped product release
(`v1_0_0` … `v1_3_0`, `v1_3_1`). Later 1.4 areas that need schema changes
extend THIS revision (as Block C extended `v1_3_1`) until 1.4 ships; it
becomes frozen on release, not before.

### The identifier `v1_4_0` had two earlier, retired meanings

During Céluma 1.3 development `v1_4_0` named two short-lived revisions on the
`celuma-1.3` branch, both folded into `v1_3_0` and deleted:
`v1_4_0_report_template_version.py` (Phase 2 Block B, 2026-07-29 → 08-03) and
`v1_4_0_notifications_core.py` (Phase 3 Block B, 2026-08-06 → 08-10). Neither
was ever on a first-parent state of `main` or in a release tag, and CI only
migrated staging from `main`/tags, so no CI-deployed database carried them;
production went `v1_2_0` → `v1_3_0` → `v1_3_1` (1.3.1 release record). The
frozen `v1_3_0` docstring and the backend README already reserved `v1_4_0` for
Céluma 1.4, and Rafael decided (2026-10-14) to use it as planned. The
migration tests now allow `v1_4_0` only as this revision, in this file, on
`v1_3_1`; every other retired id stays forbidden. A database that reports
`v1_4_0` *before* the 1.4 release window would carry a retired meaning — see
the pre-upgrade check in `celuma-engineering/runbooks/migrations.md`.

`v1_3_0` and `v1_3_1` are frozen and are NOT edited by this revision.

===========================================================================
§1 — List search and pagination (issues #5/#6/#7)
===========================================================================

Specification: `celuma-engineering/specs/lists/search-and-pagination.md`;
decision record: `celuma-engineering/adr/0011-server-side-list-search.md`.

**§1a — `fuzzystrmatch`.** Server-paginated lists apply the same matching
contract as the browser, including conservative typo tolerance for human text
("biosia" → "Biopsia"). The edit distance is `levenshtein_less_equal`, from
the `fuzzystrmatch` contrib extension. It ships with PostgreSQL (and the
official images used locally and in CI), is supported by Amazon RDS for
PostgreSQL, and is a *trusted* extension since PostgreSQL 13, so it installs
for a role with CREATE on the database — the RDS master user the API connects
as qualifies. If it cannot be created the migration fails loudly; the API
does not fall back to a weaker search.

**§1b — indexes.** None of `sample`, `order`, `patient`, `report` had an index
on `tenant_id`, and `sample.order_id`, `order.patient_id`, `report.order_id`
and `report_version.report_id` had none either. Every list therefore scanned
the whole shared table — every laboratory's rows — so a small laboratory's
list got slower as other laboratories grew. On the synthetic shared database
of the list benchmark (`scripts/list_performance/`, 18 tenants / 185k samples)
these indexes made a small tenant's first page independent of the other
tenants' volume; the measurements are in
`celuma-engineering/specs/lists/performance-baseline.md`.

Plain `CREATE INDEX` is used, not `CONCURRENTLY`: the release runbook applies
migrations in a drained window (`celuma-engineering/runbooks/migrations.md`),
and building these indexes took well under a second each at the measured
scale. No `IF NOT EXISTS`: an index of the same name created by hand would be
a surprise worth stopping for. Nothing here rewrites or backfills data.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "v1_4_0"
down_revision: Union[str, Sequence[str], None] = "v1_3_1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: (index name, table, column). Quoted table names: `order` is reserved.
LIST_INDEXES = (
    ("ix_sample_tenant_id", "sample", "tenant_id"),
    ("ix_sample_order_id", "sample", "order_id"),
    ("ix_order_tenant_id", '"order"', "tenant_id"),
    ("ix_order_patient_id", '"order"', "patient_id"),
    ("ix_patient_tenant_id", "patient", "tenant_id"),
    ("ix_report_tenant_id", "report", "tenant_id"),
    ("ix_report_order_id", "report", "order_id"),
    ("ix_report_version_report_id", "report_version", "report_id"),
)


def upgrade() -> None:
    # §1a
    op.execute("CREATE EXTENSION IF NOT EXISTS fuzzystrmatch")
    # §1b
    for name, table, column in LIST_INDEXES:
        op.execute(f"CREATE INDEX {name} ON public.{table} USING btree ({column})")


def downgrade() -> None:
    """Returns the database to the `v1_3_1` contract.

    Dropping the indexes loses no data. The extension is dropped as well: the
    1.3.1 schema did not have it, and nothing but the 1.4 list search uses it.
    """
    for name, _table, _column in reversed(LIST_INDEXES):
        op.execute(f"DROP INDEX IF EXISTS public.{name}")
    op.execute("DROP EXTENSION IF EXISTS fuzzystrmatch")
