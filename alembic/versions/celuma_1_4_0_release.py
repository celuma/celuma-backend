"""v1.4.0 - Céluma 1.4 release migration (in development)

Revision ID: celuma_1_4_0
Revises: v1_3_1
Create Date: 2026-10-14

The database revision for the Céluma 1.4 release, following the standing
contract of one Alembic revision per shipped product release. Later 1.4 areas
that need schema changes extend THIS revision (as Block C extended `v1_3_1`)
until 1.4 ships; it becomes frozen on release, not before.

### Why the revision id is not `v1_4_0`

`v1_4_0` … `v1_13_0` are retired development identifiers of Céluma 1.3
(`tests/test_alembic_migrations.py::SUPERSEDED_REVISIONS`): executable code
may not reference them, so that no database can ever be stamped with a
revision that once meant something else. `celuma_1_4_0` names the same
product release without reusing a retired id. The choice is a release-contract
decision under review together with this revision.

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
list got slower as other laboratories grew. Measured with the synthetic
benchmark (`scripts/list_performance/`) on a shared database of 18 tenants /
185k samples, the small tenant's first page went from 23 ms to 9.5 ms
(samples) and 34 ms to 9.9 ms (orders) with these indexes, and stopped
depending on the other tenants' volume.

Plain `CREATE INDEX` is used, not `CONCURRENTLY`: the release runbook applies
migrations in a drained window (`celuma-engineering/runbooks/migrations.md`),
and building these indexes took well under a second each at the measured
scale. No `IF NOT EXISTS`: an index of the same name created by hand would be
a surprise worth stopping for. Nothing here rewrites or backfills data.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "celuma_1_4_0"
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
