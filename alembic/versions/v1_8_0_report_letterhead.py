"""v1.8.0 - report_letterhead / report_letterhead_version (shared, tenant-owned)

Revision ID: v1_8_0
Revises: v1_7_0
Create Date: 2026-08-01

Post-Fase-2 remediation. Purely additive: creates `report_letterhead` and
`report_letterhead_version`, and adds two nullable FK columns
(`report_template.preferred_letterhead_version_id`,
`report_version.letterhead_version_id`). No existing table's data is
modified, no backfill runs, and no `ReportTemplateVersion` row or embedded
`rendering_snapshot` JSON is touched by this migration.

Design notes (see report-letterhead-domain-contract.md and
report-letterhead-version-contract.md for the full rationale):
  - `report_letterhead`/`report_letterhead_version` mirror the existing
    `report_template`/`report_template_version` split exactly (same
    append-only, immutable-version pattern already proven in v1_4_0) —
    this is the "safer transition" per the remediation brief: reusing a
    battle-tested shape rather than inventing a lighter/virtual model.
  - `status` is a plain VARCHAR + CHECK constraint, not a native Postgres
    ENUM, for the same evolvability reason as `report_template_version`.
  - A partial unique index guarantees at most one ACTIVE version per
    `report_letterhead_id`, mirroring `ix_report_template_version_one_active`.
  - A second partial unique index guarantees at most one `is_default=true`
    letterhead per tenant, at the database level.
  - `report_template.preferred_letterhead_version_id` is a plain nullable
    FK (not a join table): a template has at most one preferred version at
    a time, and NULL means "resolve the tenant default at creation time"
    (never "no branding").
  - `report_version.letterhead_version_id` is the administrative/audit
    twin of the existing `template_version_id` column — it does NOT
    change the `rendering_snapshot` JSON contract (still
    `{schema_version: 2, template, presentation}`); see
    remediation-architecture-decision.md for why schema_version stays at
    2 instead of bumping to 3.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "v1_8_0"
down_revision: Union[str, Sequence[str], None] = "v1_7_0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "report_letterhead",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_report_letterhead_tenant_id", "report_letterhead", ["tenant_id"])
    # At most one default letterhead per tenant, enforced at the DB level.
    op.execute(
        """
        CREATE UNIQUE INDEX ix_report_letterhead_one_default
        ON public.report_letterhead (tenant_id)
        WHERE (is_default = true)
        """
    )

    op.create_table(
        "report_letterhead_version",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_letterhead_id", postgresql.UUID(as_uuid=True), nullable=False),
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
            name="ck_report_letterhead_version_status",
        ),
        sa.CheckConstraint(
            "version_number >= 1", name="ck_report_letterhead_version_number_positive"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["report_letterhead_id"], ["report_letterhead.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "report_letterhead_id", "version_number", name="uq_report_letterhead_version_number"
        ),
    )
    op.create_index(
        "ix_report_letterhead_version_tenant_id", "report_letterhead_version", ["tenant_id"]
    )
    op.create_index(
        "ix_report_letterhead_version_report_letterhead_id",
        "report_letterhead_version",
        ["report_letterhead_id"],
    )
    op.create_index(
        "ix_report_letterhead_version_status", "report_letterhead_version", ["status"]
    )
    # At most one ACTIVE version per logical letterhead, enforced at the DB level.
    op.execute(
        """
        CREATE UNIQUE INDEX ix_report_letterhead_version_one_active
        ON public.report_letterhead_version (report_letterhead_id)
        WHERE (status = 'ACTIVE')
        """
    )

    op.add_column(
        "report_template",
        sa.Column(
            "preferred_letterhead_version_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.create_foreign_key(
        "fk_report_template_preferred_letterhead_version_id",
        "report_template",
        "report_letterhead_version",
        ["preferred_letterhead_version_id"],
        ["id"],
    )

    op.add_column(
        "report_version",
        sa.Column("letterhead_version_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_report_version_letterhead_version_id",
        "report_version",
        "report_letterhead_version",
        ["letterhead_version_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_report_version_letterhead_version_id", "report_version", type_="foreignkey"
    )
    op.drop_column("report_version", "letterhead_version_id")

    op.drop_constraint(
        "fk_report_template_preferred_letterhead_version_id",
        "report_template",
        type_="foreignkey",
    )
    op.drop_column("report_template", "preferred_letterhead_version_id")

    op.execute("DROP INDEX IF EXISTS ix_report_letterhead_version_one_active")
    op.drop_index(
        "ix_report_letterhead_version_status", table_name="report_letterhead_version"
    )
    op.drop_index(
        "ix_report_letterhead_version_report_letterhead_id",
        table_name="report_letterhead_version",
    )
    op.drop_index(
        "ix_report_letterhead_version_tenant_id", table_name="report_letterhead_version"
    )
    op.drop_table("report_letterhead_version")

    op.execute("DROP INDEX IF EXISTS ix_report_letterhead_one_default")
    op.drop_index("ix_report_letterhead_tenant_id", table_name="report_letterhead")
    op.drop_table("report_letterhead")
