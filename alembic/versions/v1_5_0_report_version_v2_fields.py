"""v1.5.0 - report_version V2 metadata (schema_version, template_version_id, generated_by_renderer_version)

Revision ID: v1_5_0
Revises: v1_4_0
Create Date: 2026-07-29

Céluma 1.3 Fase 2, Bloque B, Historia B5. Purely additive:

  - `schema_version` (nullable int): NULL for every existing row (legacy).
    Only ever written as 2 for reports created while
    `Tenant.reports_v2_enabled` is true (Historia B6). Never backfilled.
  - `template_version_id` (nullable FK -> report_template_version.id):
    required in application code whenever schema_version = 2, enforced here
    additionally with a CHECK constraint. ON DELETE is intentionally left
    unset (NO ACTION/RESTRICT): a report_template_version referenced by any
    report_version can never be hard-deleted, only archived.
  - `generated_by_renderer_version` (nullable string): free-form identifier
    of the renderer build that produced this version's document, for future
    audit (e.g. "versioned-v2.1.0"). Not read anywhere in this block.

No existing `report_version` row is modified by this migration — no
backfill, no computed schema_version, no invented template reference.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "v1_5_0"
down_revision: Union[str, Sequence[str], None] = "v1_4_0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "report_version",
        sa.Column("schema_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "report_version",
        sa.Column("template_version_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "report_version",
        sa.Column("generated_by_renderer_version", sa.String(length=100), nullable=True),
    )
    op.create_foreign_key(
        "report_version_template_version_id_fkey",
        source_table="report_version",
        referent_table="report_template_version",
        local_cols=["template_version_id"],
        remote_cols=["id"],
    )
    op.create_index(
        "ix_report_version_template_version_id", "report_version", ["template_version_id"]
    )
    op.create_index("ix_report_version_schema_version", "report_version", ["schema_version"])
    # Application-level invariant, also enforced in the database: a V2
    # report_version (schema_version = 2) must always carry a
    # template_version_id. Legacy rows (schema_version IS NULL) are exempt.
    op.create_check_constraint(
        "ck_report_version_v2_requires_template_version",
        "report_version",
        "schema_version IS DISTINCT FROM 2 OR template_version_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_report_version_v2_requires_template_version", "report_version", type_="check"
    )
    op.drop_index("ix_report_version_schema_version", table_name="report_version")
    op.drop_index("ix_report_version_template_version_id", table_name="report_version")
    op.drop_constraint(
        "report_version_template_version_id_fkey", "report_version", type_="foreignkey"
    )
    op.drop_column("report_version", "generated_by_renderer_version")
    op.drop_column("report_version", "template_version_id")
    op.drop_column("report_version", "schema_version")
