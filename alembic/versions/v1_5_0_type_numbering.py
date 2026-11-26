"""v1.5.0 - Optional numbering per study type and sample type (#64); catalog type removal (#94)

Revision ID: v1_5_0
Revises: v1_4_1
Create Date: 2026-11-24

The Céluma 1.5 release revision. `v1_4_1` shipped with Céluma 1.4.1 and is not
edited; following the standing contract of one revision per product release,
1.5 schema changes go here while 1.5 is unreleased.

Specification: `celuma-engineering/specs/laboratory/type-numbering.md`;
decision record: `celuma-engineering/adr/0023-type-numbering-overrides.md`.

**`numbering_type_config`.** One OPTIONAL row per tenant, entity kind and
catalog type (study type for orders, sample type for samples) holding the only
settings a type may change: `format`, `sequence_min_width`, `start_value`.
Activation, counter scope and timezone stay in `numbering_config`. The table is
created EMPTY: no row means "use the laboratory's numbering", which is exactly
the 1.4 behaviour, so the upgrade changes no laboratory and rewrites no row.
`type_id` has no foreign key on purpose (one column serves two catalogs, like
`numbering_counter.scope_id`); the API checks tenant ownership and the
hard-delete routes remove the row with the type.

**§2 — catalog type removal (#94).** `study_type` and `sample_type` gain three
NULLABLE columns, `removed_at`, `removed_by` (FK `app_user`), `removal_reason`
(≤ 500) and `code_released_at`, plus the checks `removed_at IS NULL OR
is_active = false` (a removed type can never be active) and `code_released_at IS
NULL OR removed_at IS NOT NULL` (only a removed type releases its code). The
removal releases the code only when no order (study type) or sample (sample type),
withdrawn ones included, references the type; the row keeps its original code.
`sample_type`'s full `uq_sample_type_tenant_code` becomes the partial unique index
`uq_sample_type_tenant_code_reserved ... WHERE code_released_at IS NULL` (still
one reserving row per tenant and code). Every existing row gets `NULL`: nothing is
removed or released by the upgrade and every type behaves exactly as before. A removed type keeps its row,
id, code and every reference. Specification:
`celuma-engineering/specs/laboratory/catalog-type-removal.md`; proposed decision:
`celuma-engineering/adr/0025-catalog-type-removal.md`.

**Locks.** A new, empty table: no lock on any existing business table beyond
the FK checks against `tenant` and `app_user` (SHARE ROW EXCLUSIVE, brief).
§2 builds the partial index on `sample_type` (a small catalog table, under the
same brief lock) and takes ACCESS EXCLUSIVE on `study_type` and `sample_type` briefly: adding a
nullable column without a default is catalog-only in PostgreSQL ≥ 11, and the
check constraint is added `NOT VALID` and then validated (a scan of two small
catalog tables, under SHARE UPDATE EXCLUSIVE).

**Downgrade** drops the table: type configurations are lost and every type
numbers with the laboratory's configuration again. Issued codes, counters and
the ledger are untouched. §2's downgrade REFUSES (raises, nothing changed) when a released code was reused,
i.e. two rows of one catalog share a tenant and code: the pre-1.5 schema
(`uq_sample_type_tenant_code`) and the pre-1.5 API (one study type per code)
cannot represent that history, and no row is rewritten to make it fit; restore
the pre-upgrade snapshot instead. Otherwise it restores the full constraint and
drops the columns and checks:
a removed type then becomes an ordinary INACTIVE type (it already is inactive),
so it stays out of new-record selectors, but the removal marker, its author and
reason are lost from the row (the `*.REMOVED` audit rows remain), and an
ordinary edit could reactivate it. A 1.4.x image does NOT start on a `v1_5_0` database
(its `start.sh` head check cannot locate this revision); roll back with this
downgrade run by the 1.5 image, or restore the pre-upgrade snapshot. Never stamp.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "v1_5_0"
down_revision: Union[str, Sequence[str], None] = "v1_4_1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE = "numbering_type_config"
#: §2 (#94): the two catalogs that gain a removal marker.
CATALOGS = ("study_type", "sample_type")


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE public.{TABLE} (
            tenant_id uuid NOT NULL REFERENCES public.tenant(id),
            entity_kind varchar(16) NOT NULL,
            type_id uuid NOT NULL,
            format varchar(60) NOT NULL,
            sequence_min_width integer NOT NULL,
            start_value integer NOT NULL,
            created_at timestamp without time zone NOT NULL,
            created_by uuid REFERENCES public.app_user(id),
            updated_at timestamp without time zone NOT NULL,
            updated_by uuid REFERENCES public.app_user(id),
            CONSTRAINT pk_numbering_type_config PRIMARY KEY (tenant_id, entity_kind, type_id),
            CONSTRAINT ck_numbering_type_config_kind CHECK (entity_kind IN ('order', 'sample')),
            CONSTRAINT ck_numbering_type_config_width CHECK (sequence_min_width BETWEEN 1 AND 6),
            CONSTRAINT ck_numbering_type_config_start CHECK (start_value BETWEEN 1 AND 99999999)
        )
        """
    )
    # §2 (#94) — catalog type removal: a retained tombstone, distinct from deactivation.
    for table in CATALOGS:
        op.execute(
            f"""
            ALTER TABLE public.{table}
                ADD COLUMN removed_at timestamp without time zone,
                ADD COLUMN removed_by uuid REFERENCES public.app_user(id),
                ADD COLUMN removal_reason varchar(500),
                ADD COLUMN code_released_at timestamp without time zone
            """
        )
        op.execute(
            f"ALTER TABLE public.{table} ADD CONSTRAINT ck_{table}_removed_inactive "
            "CHECK (removed_at IS NULL OR is_active = false) NOT VALID"
        )
        op.execute(f"ALTER TABLE public.{table} VALIDATE CONSTRAINT ck_{table}_removed_inactive")
        op.execute(
            f"ALTER TABLE public.{table} ADD CONSTRAINT ck_{table}_code_release_removed "
            "CHECK (code_released_at IS NULL OR removed_at IS NOT NULL)"
        )
    # Unique among code-reserving rows only (owner decision 2026-11-25: an unused removed type frees its code).
    op.execute("ALTER TABLE public.sample_type DROP CONSTRAINT uq_sample_type_tenant_code")
    op.execute(
        "CREATE UNIQUE INDEX uq_sample_type_tenant_code_reserved ON public.sample_type (tenant_id, code) "
        "WHERE code_released_at IS NULL"
    )


def downgrade() -> None:
    conn = op.get_bind()
    for table in CATALOGS:
        reused = conn.exec_driver_sql(
            f"SELECT count(*) FROM (SELECT 1 FROM public.{table} GROUP BY tenant_id, upper(code) "
            "HAVING count(*) > 1 AND bool_or(code_released_at IS NOT NULL)) d"
        ).scalar()
        if reused:
            raise RuntimeError(
                f"v1_5_0 downgrade refused: {reused} code(s) of {table} were released by a removal and reused by "
                "another type. The pre-1.5 schema cannot hold both rows without rewriting history; restore the "
                "pre-upgrade snapshot instead (nothing was changed)."
            )
    op.execute("DROP INDEX public.uq_sample_type_tenant_code_reserved")
    op.execute(
        "ALTER TABLE public.sample_type ADD CONSTRAINT uq_sample_type_tenant_code UNIQUE (tenant_id, code)"
    )
    for table in reversed(CATALOGS):
        op.execute(f"ALTER TABLE public.{table} DROP CONSTRAINT ck_{table}_code_release_removed")
        op.execute(f"ALTER TABLE public.{table} DROP CONSTRAINT ck_{table}_removed_inactive")
        op.execute(
            f"ALTER TABLE public.{table} DROP COLUMN code_released_at, DROP COLUMN removal_reason, "
            "DROP COLUMN removed_by, DROP COLUMN removed_at"
        )
    op.execute(f"DROP TABLE public.{TABLE}")
