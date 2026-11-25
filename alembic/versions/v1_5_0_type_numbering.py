"""v1.5.0 - Optional numbering per study type and sample type (Céluma 1.5, issue #64)

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

**Locks.** A new, empty table: no lock on any existing business table beyond
the FK checks against `tenant` and `app_user` (SHARE ROW EXCLUSIVE, brief).

**Downgrade** drops the table: type configurations are lost and every type
numbers with the laboratory's configuration again. Issued codes, counters and
the ledger are untouched. A 1.4.x image does NOT start on a `v1_5_0` database
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


def downgrade() -> None:
    op.execute(f"DROP TABLE public.{TABLE}")
