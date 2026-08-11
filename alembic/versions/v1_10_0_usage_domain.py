"""v1.10.0 - Céluma 1.3, Phase 4, Block B: usage domain model

Revision ID: v1_10_0
Revises: v1_3_0
Create Date: 2026-08-10

Purely additive schema for the tenant-usage domain
(docs/celuma-1.3/phase-4-block-b/database-migration-notes.md has the full
narrative). This is the first revision on top of the closed `v1_3_0`
release migration — `v1_3_0` itself is not touched.

Revision id note: `v1_4_0` through `v1_9_0` are permanently retired —
`tests/test_alembic_migrations.py::SUPERSEDED_REVISIONS` blacklists them
because the Phase 3 closure squash folded them into `v1_3_0` and they must
never be resolvable again by any executable code. `v1_10_0` is the first
id after `v1_3_0` that was never used by that superseded chain, so it is
free to be the real next revision.

Three new tables, all empty after this migration runs (no backfill, no
seed row, matching every prior Céluma 1.3 revision's "additive means
additive" discipline):

  1. `tenant_usage`                — one row per tenant once usage tracking
                                      is initialized (Block C). Absence of a
                                      row means "not yet initialized", not
                                      "zero usage" — see the model docstring
                                      in app/models/tenant_usage.py.
  2. `tenant_limits`                — one optional row per tenant. Absence
                                      of a row means "no limits configured"
                                      (both storage and user limits
                                      unlimited).
  3. `tenant_usage_reconciliation`  — append-only reconciliation-run
                                      history (Block D owns the engine that
                                      writes to it; this revision only
                                      creates the table).

Also adds one index Block A's performance assessment flagged as unverified
and worth confirming before Block B's `active_internal_users` query ships
on every usage-dashboard load: `app_user(tenant_id, is_active)`. Confirmed
absent before this migration (`app_user` previously carried only
`ix_app_user_email` and `ix_app_user_username`, per
alembic/versions/v1_0_0_initial_schema.py).

No storage_object.tenant_id backfill, no S3 operation, no data mutation of
any existing row happens in this revision — see
docs/celuma-1.3/phase-4-block-b/database-migration-notes.md §"What this
revision does not do".
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "v1_10_0"
down_revision: Union[str, Sequence[str], None] = "v1_3_0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_RECONCILIATION_STATUSES = ("RUNNING", "SUCCEEDED", "FAILED")


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. tenant_usage — fast-path billable storage counter
    # ------------------------------------------------------------------
    op.create_table(
        "tenant_usage",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "billable_storage_bytes",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("last_updated", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "billable_storage_bytes >= 0",
            name="ck_tenant_usage_storage_non_negative",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.PrimaryKeyConstraint("tenant_id"),
    )

    # ------------------------------------------------------------------
    # 2. tenant_limits — configured, optional per-tenant ceilings
    # ------------------------------------------------------------------
    op.create_table(
        "tenant_limits",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("storage_limit_bytes", sa.BigInteger(), nullable=True),
        sa.Column("user_limit", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "storage_limit_bytes IS NULL OR storage_limit_bytes > 0",
            name="ck_tenant_limits_storage_limit_positive",
        ),
        sa.CheckConstraint(
            "user_limit IS NULL OR user_limit > 0",
            name="ck_tenant_limits_user_limit_positive",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.PrimaryKeyConstraint("tenant_id"),
    )

    # ------------------------------------------------------------------
    # 3. tenant_usage_reconciliation — append-only run history
    # ------------------------------------------------------------------
    op.create_table(
        "tenant_usage_reconciliation",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="RUNNING",
        ),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("expected_storage_bytes", sa.BigInteger(), nullable=True),
        sa.Column("actual_storage_bytes", sa.BigInteger(), nullable=True),
        sa.Column("difference_bytes", sa.BigInteger(), nullable=True),
        sa.Column("objects_checked", sa.BigInteger(), nullable=True),
        sa.Column("orphans_found", sa.BigInteger(), nullable=True),
        sa.Column("missing_objects_found", sa.BigInteger(), nullable=True),
        sa.Column("repaired", sa.Boolean(), nullable=True),
        sa.Column("error_code", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in _RECONCILIATION_STATUSES)})",
            name="ck_tenant_usage_reconciliation_status",
        ),
        # RUNNING must not yet have a completion timestamp.
        sa.CheckConstraint(
            "status <> 'RUNNING' OR completed_at IS NULL",
            name="ck_tenant_usage_reconciliation_running_no_completed_at",
        ),
        # A terminal status (SUCCEEDED/FAILED) must carry a completion timestamp.
        sa.CheckConstraint(
            "status = 'RUNNING' OR completed_at IS NOT NULL",
            name="ck_tenant_usage_reconciliation_terminal_requires_completed_at",
        ),
        # A successful run should not be carrying a sanitized error code.
        sa.CheckConstraint(
            "status <> 'SUCCEEDED' OR error_code IS NULL",
            name="ck_tenant_usage_reconciliation_succeeded_no_error_code",
        ),
        sa.CheckConstraint(
            "objects_checked IS NULL OR objects_checked >= 0",
            name="ck_tenant_usage_reconciliation_objects_checked_non_negative",
        ),
        sa.CheckConstraint(
            "orphans_found IS NULL OR orphans_found >= 0",
            name="ck_tenant_usage_reconciliation_orphans_found_non_negative",
        ),
        sa.CheckConstraint(
            "missing_objects_found IS NULL OR missing_objects_found >= 0",
            name="ck_tenant_usage_reconciliation_missing_objects_non_negative",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    # "Latest reconciliation for tenant" — the one query Block B's own
    # UsageService.get_latest_reconciliation() issues.
    op.create_index(
        "ix_tenant_usage_reconciliation_tenant_started_at",
        "tenant_usage_reconciliation",
        ["tenant_id", "started_at"],
    )
    # Serves a future Block D query: "RUNNING reconciliations older than a
    # staleness threshold", for crash recovery. Not used by anything in
    # Block B; documented here because it ships in this revision.
    op.create_index(
        "ix_tenant_usage_reconciliation_status_started_at",
        "tenant_usage_reconciliation",
        ["status", "started_at"],
    )

    # ------------------------------------------------------------------
    # 4. app_user(tenant_id, is_active) — confirmed absent (see module
    #    docstring); added because active_internal_users/
    #    active_physician_portal_users run this predicate on every
    #    usage-dashboard load once a consumer exists.
    # ------------------------------------------------------------------
    op.create_index(
        "ix_app_user_tenant_id_is_active", "app_user", ["tenant_id", "is_active"]
    )


def downgrade() -> None:
    op.drop_index("ix_app_user_tenant_id_is_active", table_name="app_user")

    op.drop_index(
        "ix_tenant_usage_reconciliation_status_started_at",
        table_name="tenant_usage_reconciliation",
    )
    op.drop_index(
        "ix_tenant_usage_reconciliation_tenant_started_at",
        table_name="tenant_usage_reconciliation",
    )
    op.drop_table("tenant_usage_reconciliation")

    op.drop_table("tenant_limits")

    op.drop_table("tenant_usage")
