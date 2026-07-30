"""v1.4.0 - report_template_version (append-only, immutable)

Revision ID: v1_4_0
Revises: v1_3_0
Create Date: 2026-07-29

Céluma 1.3 Fase 2, Bloque B, Historia B2. Purely additive: creates the
`report_template_version` table. No existing table is modified. No report is
created with a reference to this table in this migration (`reports_v2_enabled`
stays false for every tenant — see database-migration-notes.md, Bloque B).

Design notes (see report-template-version-contract.md for the full rationale):
  - `status` is stored as a plain VARCHAR + CHECK constraint, not a Postgres
    native ENUM type, so future states can be added with a simple
    constraint change instead of `ALTER TYPE ... ADD VALUE` (which cannot
    run inside a transaction on older Postgres and is generally awkward to
    evolve).
  - A partial unique index guarantees at most one ACTIVE version per
    `report_template_id` at the database level, not just in application code.
  - `(report_template_id, version_number)` is unique to guarantee correct,
    gap-tolerant sequential numbering even under concurrent publishes.
  - `report_template_id` has no ON DELETE clause, which defaults to Postgres
    NO ACTION/RESTRICT: a `ReportTemplate` with published versions cannot be
    hard-deleted while any version still references it (see the companion
    application-level guard added to `DELETE /reports/templates/{id}` in
    this same block).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "v1_4_0"
down_revision: Union[str, Sequence[str], None] = "v1_3_0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "report_template_version",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="PUBLISHED",
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('PUBLISHED', 'ACTIVE', 'ARCHIVED')",
            name="ck_report_template_version_status",
        ),
        sa.CheckConstraint("version_number >= 1", name="ck_report_template_version_number_positive"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["report_template_id"], ["report_template.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "report_template_id", "version_number", name="uq_report_template_version_number"
        ),
    )
    op.create_index(
        "ix_report_template_version_tenant_id", "report_template_version", ["tenant_id"]
    )
    op.create_index(
        "ix_report_template_version_report_template_id",
        "report_template_version",
        ["report_template_id"],
    )
    op.create_index(
        "ix_report_template_version_status", "report_template_version", ["status"]
    )
    # At most one ACTIVE version per logical template, enforced at the DB level.
    op.execute(
        """
        CREATE UNIQUE INDEX ix_report_template_version_one_active
        ON public.report_template_version (report_template_id)
        WHERE (status = 'ACTIVE')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_report_template_version_one_active")
    op.drop_index("ix_report_template_version_status", table_name="report_template_version")
    op.drop_index(
        "ix_report_template_version_report_template_id", table_name="report_template_version"
    )
    op.drop_index("ix_report_template_version_tenant_id", table_name="report_template_version")
    op.drop_table("report_template_version")
