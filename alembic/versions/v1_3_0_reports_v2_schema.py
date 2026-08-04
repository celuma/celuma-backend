"""v1.3.0 - Céluma 1.3 release schema (versioned reports, letterheads, official PDF)

Revision ID: v1_3_0
Revises: v1_2_0
Create Date: 2026-08-03

Single release migration for Céluma 1.3. It consolidates the complete
database delta that was developed across Phase 2 (Blocks A–E) and the five
post-Phase-2 remediation rounds, which lived on the `celuma-1.3` branch as
the chain v1_3_0 → v1_4_0 → v1_5_0 → v1_6_0 → v1_7_0 → v1_8_0 → v1_9_0.
None of those intermediate revisions was ever applied to production,
staging, or any customer database (see
docs/celuma-1.3/phase-2-closure/alembic-squash-inventory.md), so the release
ships as one contractual migration on top of v1_2_0 instead of seven.

Delta, in application order:

  1. `tenant.reports_v2_enabled`            (Block A, ex-v1_3_0)
  2. `report_template_version`              (Block B, ex-v1_4_0)
  3. `report_version` V2 metadata           (Block B, ex-v1_5_0)
  4. `storage_object.tenant_id`             (Block C, ex-v1_6_0)
  5. `report_version` PDF artifact fields   (Block E, ex-v1_7_0)
  6. `report_letterhead[_version]`          (remediation 1, ex-v1_8_0)
  7. `preferred_letterhead_id` + publish lock (remediation 2, ex-v1_9_0)

Design notes preserved verbatim from the superseded revisions — the full
rationale lives in the per-block contracts under docs/celuma-1.3/:

  - `status` on both version tables is a plain VARCHAR + CHECK, not a native
    Postgres ENUM, so future states can be added with a constraint change
    instead of `ALTER TYPE ... ADD VALUE` (which cannot run inside a
    transaction on older Postgres and is awkward to revert).
  - Partial unique indexes enforce "at most one ACTIVE version" per logical
    template/letterhead and "at most one default letterhead" per tenant at
    the database level, not only in application code.
  - No FK carries an ON DELETE clause, which defaults to Postgres NO
    ACTION/RESTRICT: a template/letterhead version referenced by any report
    can never be hard-deleted, only archived.
  - `ck_report_version_pdf_ready_requires_artifact` is the DB-level backstop
    for Block E's core invariant: no `READY` PDF without a real, hashed,
    persisted artifact.

Historical-compatibility decisions, deliberately preserved:

  - Every column added to a preexisting table is nullable, except
    `tenant.reports_v2_enabled`, whose `server_default=false` backfills
    existing rows inside the same ALTER TABLE.
  - NO backfill runs anywhere in this migration. Specifically: no
    `schema_version` is inferred for existing `report_version` rows, no
    `pdf_generation_status`/`pdf_sha256` is invented for PDFs that may sit
    behind `pdf_storage_id` from the old ad-hoc upload endpoints, no
    `storage_object.tenant_id` is derived from bucket/key, and no
    `letterhead_version_id` is assigned to historical V2 reports.
  - No `rendering_snapshot` JSON is read or rewritten.
  - The letterhead tables and their FK references are purely additive, so
    reports created before letterhead entities existed keep
    `letterhead_version_id = NULL` and remain readable, editable and
    submittable to review.

Column addition order within `report_version` and `report_template` matches
the order of the superseded chain, so the resulting physical column order is
identical to what the chain produced.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "v1_3_0"
down_revision: Union[str, Sequence[str], None] = "v1_2_0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Tenant-level V2 feature flag (ex-v1_3_0, Phase 2 Block A)
    # ------------------------------------------------------------------
    op.add_column(
        "tenant",
        sa.Column(
            "reports_v2_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    # ------------------------------------------------------------------
    # 2. report_template_version — append-only, immutable (ex-v1_4_0, Block B)
    # ------------------------------------------------------------------
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
        sa.CheckConstraint(
            "version_number >= 1", name="ck_report_template_version_number_positive"
        ),
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

    # ------------------------------------------------------------------
    # 3. report_version V2 metadata (ex-v1_5_0, Block B)
    # ------------------------------------------------------------------
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
    # A V2 report_version (schema_version = 2) must always carry a
    # template_version_id. Legacy rows (schema_version IS NULL) are exempt.
    op.create_check_constraint(
        "ck_report_version_v2_requires_template_version",
        "report_version",
        "schema_version IS DISTINCT FROM 2 OR template_version_id IS NOT NULL",
    )

    # ------------------------------------------------------------------
    # 4. storage_object.tenant_id — nullable ownership tag (ex-v1_6_0, Block C)
    # ------------------------------------------------------------------
    op.add_column(
        "storage_object",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "storage_object_tenant_id_fkey",
        source_table="storage_object",
        referent_table="tenant",
        local_cols=["tenant_id"],
        remote_cols=["id"],
    )
    op.create_index("ix_storage_object_tenant_id", "storage_object", ["tenant_id"])

    # ------------------------------------------------------------------
    # 5. report_version PDF artifact fields (ex-v1_7_0, Block E)
    # ------------------------------------------------------------------
    op.add_column(
        "report_version",
        sa.Column("pdf_generation_status", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "report_version",
        sa.Column("pdf_generation_started_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "report_version",
        sa.Column("pdf_generated_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "report_version",
        sa.Column("pdf_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "report_version",
        sa.Column("pdf_size_bytes", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "report_version",
        sa.Column("pdf_page_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "report_version",
        sa.Column("pdf_generator_version", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "report_version",
        sa.Column("pdf_error_code", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "report_version",
        sa.Column("pdf_error_message", sa.String(length=500), nullable=True),
    )
    op.create_index(
        "ix_report_version_pdf_generation_status",
        "report_version",
        ["pdf_generation_status"],
    )
    op.create_check_constraint(
        "ck_report_version_pdf_generation_status_values",
        "report_version",
        "pdf_generation_status IS NULL OR pdf_generation_status IN "
        "('GENERATING', 'READY', 'FAILED')",
    )
    op.create_check_constraint(
        "ck_report_version_pdf_ready_requires_artifact",
        "report_version",
        "pdf_generation_status IS DISTINCT FROM 'READY' OR "
        "(pdf_storage_id IS NOT NULL AND pdf_sha256 IS NOT NULL AND "
        "pdf_size_bytes IS NOT NULL AND pdf_page_count IS NOT NULL)",
    )

    # ------------------------------------------------------------------
    # 6. report_letterhead / report_letterhead_version (ex-v1_8_0, remediation 1)
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # 7. preferred_letterhead_id + publish lock (ex-v1_9_0, remediation 2)
    # ------------------------------------------------------------------
    op.add_column(
        "report_template",
        sa.Column("preferred_letterhead_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_report_template_preferred_letterhead_id",
        "report_template",
        "report_letterhead",
        ["preferred_letterhead_id"],
        ["id"],
    )

    op.add_column(
        "report_version",
        sa.Column("publish_started_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "report_version",
        sa.Column("publish_started_by", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_report_version_publish_started_by",
        "report_version",
        "app_user",
        ["publish_started_by"],
        ["id"],
    )


def downgrade() -> None:
    """Reverse the full 1.3 delta in reverse dependency order.

    Data loss is expected and confined to columns/tables this release
    introduced: any V2 report metadata, PDF integrity metadata, letterhead
    definitions and publish claims are dropped. Preexisting (pre-1.3)
    columns and rows are untouched, and no S3 object is deleted — a
    downgraded deployment simply loses the ability to read that metadata
    until it upgrades again.
    """
    # 7. preferred_letterhead_id + publish lock
    op.drop_constraint(
        "fk_report_version_publish_started_by", "report_version", type_="foreignkey"
    )
    op.drop_column("report_version", "publish_started_by")
    op.drop_column("report_version", "publish_started_at")

    op.drop_constraint(
        "fk_report_template_preferred_letterhead_id", "report_template", type_="foreignkey"
    )
    op.drop_column("report_template", "preferred_letterhead_id")

    # 6. report_letterhead / report_letterhead_version
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

    # 5. report_version PDF artifact fields
    op.drop_constraint(
        "ck_report_version_pdf_ready_requires_artifact", "report_version", type_="check"
    )
    op.drop_constraint(
        "ck_report_version_pdf_generation_status_values", "report_version", type_="check"
    )
    op.drop_index(
        "ix_report_version_pdf_generation_status", table_name="report_version"
    )
    op.drop_column("report_version", "pdf_error_message")
    op.drop_column("report_version", "pdf_error_code")
    op.drop_column("report_version", "pdf_generator_version")
    op.drop_column("report_version", "pdf_page_count")
    op.drop_column("report_version", "pdf_size_bytes")
    op.drop_column("report_version", "pdf_sha256")
    op.drop_column("report_version", "pdf_generated_at")
    op.drop_column("report_version", "pdf_generation_started_at")
    op.drop_column("report_version", "pdf_generation_status")

    # 4. storage_object.tenant_id
    op.drop_index("ix_storage_object_tenant_id", table_name="storage_object")
    op.drop_constraint("storage_object_tenant_id_fkey", "storage_object", type_="foreignkey")
    op.drop_column("storage_object", "tenant_id")

    # 3. report_version V2 metadata
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

    # 2. report_template_version
    op.execute("DROP INDEX IF EXISTS ix_report_template_version_one_active")
    op.drop_index("ix_report_template_version_status", table_name="report_template_version")
    op.drop_index(
        "ix_report_template_version_report_template_id", table_name="report_template_version"
    )
    op.drop_index("ix_report_template_version_tenant_id", table_name="report_template_version")
    op.drop_table("report_template_version")

    # 1. tenant.reports_v2_enabled
    op.drop_column("tenant", "reports_v2_enabled")
