"""v1.3.0 - Céluma 1.3 release schema (versioned reports, letterheads, official
PDF, notifications)

Revision ID: v1_3_0
Revises: v1_2_0
Create Date: 2026-08-03
Amended: 2026-08-10 (Phase 3 closure — absorbs the Phase 3 notification chain)

The single contractual database migration for release Céluma 1.3. It carries
the complete 1.3 delta, developed across two squashes:

  - **Phase 2 closure** folded in the chain that lived on the `celuma-1.3`
    branch as v1_3_0 → v1_4_0 → v1_5_0 → v1_6_0 → v1_7_0 → v1_8_0 → v1_9_0
    (Phase 2 Blocks A–E plus five post-Phase-2 remediation rounds).
  - **Phase 3 closure** folded in the notification chain that reused three of
    those freed identifiers: v1_4_0 → v1_5_0 → v1_6_0 (Phase 3 Blocks B, D
    and F).

Development history and release history are deliberately different, and the
distinction matters when reading the per-block documents under
docs/celuma-1.3/ — those record what actually happened while 1.3 was built:

    development history:  v1_3_0 → v1_4_0 → v1_5_0 → v1_6_0
    release history:      v1_3_0 only

Because both squashes reused the same identifiers, an `ex-v1_x_0` marker below
is always qualified by its phase. No revision folded in by either squash ever
reached production, staging or a customer database — the evidence is in
docs/celuma-1.3/phase-2-closure/alembic-squash-inventory.md and
docs/celuma-1.3/phase-3-closure/phase-3-alembic-squash-inventory.md.

Delta, in application order:

   1. `tenant.reports_v2_enabled`            (Phase 2 Block A, ex-v1_3_0)
   2. `report_template_version`              (Phase 2 Block B, ex-v1_4_0)
   3. `report_version` V2 metadata           (Phase 2 Block B, ex-v1_5_0)
   4. `storage_object.tenant_id`             (Phase 2 Block C, ex-v1_6_0)
   5. `report_version` PDF artifact fields   (Phase 2 Block E, ex-v1_7_0)
   6. `report_letterhead[_version]`          (remediation 1, ex-v1_8_0)
   7. `preferred_letterhead_id` + publish lock (remediation 2, ex-v1_9_0)
   8. `notification`                         (Phase 3 Block B, ex-v1_4_0;
                                              `locale` from Block F,
                                              ex-v1_6_0)
   9. `notification_recipient`               (Phase 3 Block B, ex-v1_4_0)
  10. `notification_delivery`                (Phase 3 Block B, ex-v1_4_0,
                                              with Block D's final uniqueness
                                              model, ex-v1_5_0)
  11. `notification_preference`              (Phase 3 Block B, ex-v1_4_0)

Sections 8–11 are written in **final form**, not as a replay of how they were
developed. Two intermediate states are therefore absent by design:

  - `notification.locale` is created as part of `CREATE TABLE notification`
    rather than added by a later `ALTER TABLE`. The column is NOT NULL with
    `server_default 'es-MX'`; on a clean database there is no row to backfill,
    and the default is kept so a hand-inserted debugging row stays consistent.
  - `notification_delivery` is created directly with Block D's two partial
    unique indexes. The single `UNIQUE (notification_id, channel,
    recipient_address)` constraint that Block B created and Block D dropped is
    never created here — it encoded the assumption that one address belongs to
    one person, which is false of a laboratory, and reproducing it only to drop
    it would replay a defect.

Design notes preserved verbatim from the superseded revisions — the full
rationale lives in the per-block contracts under docs/celuma-1.3/:

  - `status` on both version tables, and every notification enum, is a plain
    VARCHAR + CHECK, not a native Postgres ENUM, so future states can be added
    with a constraint change instead of `ALTER TYPE ... ADD VALUE` (which
    cannot run inside a transaction on older Postgres and is awkward to
    revert). This is what makes it cheap to ship
    `notification_delivery.channel` with EMAIL as its only permitted value.
  - Partial unique indexes enforce "at most one ACTIVE version" per logical
    template/letterhead, "at most one default letterhead" per tenant, and one
    delivery per (event, channel, recipient) at the database level, not only
    in application code.
  - No FK carries an ON DELETE clause, which defaults to Postgres NO
    ACTION/RESTRICT: a template/letterhead version referenced by any report
    can never be hard-deleted, only archived, and deleting a user or tenant
    that still owns notification history is refused rather than silently
    erasing the audit trail.
  - `ck_report_version_pdf_ready_requires_artifact` is the DB-level backstop
    for Block E's core invariant: no `READY` PDF without a real, hashed,
    persisted artifact.
  - `UNIQUE (tenant_id, idempotency_key)` on `notification` is the
    load-bearing idempotency guarantee — a real database constraint, so
    concurrent inserts of the same occurrence are serialized by PostgreSQL.
  - `notification.resource_type` carries NO check constraint and
    `resource_id` NO foreign key: the pair is polymorphic, exactly like
    `audit_log.entity_type`/`entity_id`, so a new resource type never needs a
    migration.

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
  - The four notification tables arrive empty. In particular NO
    `notification_preference` row is seeded, per user or per type — absence of
    a row is what "use the default" means, and the whole preference contract
    depends on that staying true.

Column addition order within `report_version` and `report_template` matches
the order of the superseded chain, and `notification.locale` is the table's
last column exactly as the `ALTER TABLE` left it, so the resulting physical
column order is identical to what the chain produced.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "v1_3_0"
down_revision: Union[str, Sequence[str], None] = "v1_2_0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Notification-domain enum values, kept as literals rather than imported from
#: app.models so the migration stays a frozen historical record: a later edit
#: to the Python enum must not retroactively change what this revision created.
_NOTIFICATION_TYPES = (
    "REPORT_SUBMITTED",
    "REPORT_PDF_READY",
    "REPORT_PUBLISHED",
    "REPORT_RETRACTED",
    "ASSIGNMENT_ADDED",
    "SAMPLE_STATUS_CHANGED",
)
_SEVERITIES = ("INFO", "WARNING", "ACTION_REQUIRED")
_RECIPIENT_STATUSES = ("UNREAD", "READ", "DISMISSED")
_CHANNELS = ("EMAIL",)
_DELIVERY_STATUSES = ("PENDING", "SENDING", "SENT", "FAILED")

#: The locale every notification created before Céluma 1.3 shipped was
#: rendered in — the only one the template registry could produce.
_DEFAULT_LOCALE = "es-MX"


def _in_list(column: str, values: Sequence[str]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Tenant-level V2 feature flag (Phase 2 Block A, ex-v1_3_0)
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
    # 2. report_template_version — append-only, immutable
    #    (Phase 2 Block B, ex-v1_4_0)
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
    # 3. report_version V2 metadata (Phase 2 Block B, ex-v1_5_0)
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
    # 4. storage_object.tenant_id — nullable ownership tag
    #    (Phase 2 Block C, ex-v1_6_0)
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
    # 5. report_version PDF artifact fields (Phase 2 Block E, ex-v1_7_0)
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

    # ------------------------------------------------------------------
    # 8. notification — shared, immutable event record
    #    (Phase 3 Block B, ex-v1_4_0; `locale` from Block F, ex-v1_6_0)
    #
    # `locale` is created here, as the table's last column, rather than added
    # by a later ALTER TABLE. It records the locale the frozen title/body were
    # rendered in, which the delivery worker needs — it re-renders the email
    # from template_key/template_params instead of copying the in-app text —
    # and which audit cannot reconstruct from stored Spanish strings once a
    # second locale exists. VARCHAR(35) is the bound app/services/locale.py
    # enforces, wide enough for a BCP-47 tag with a script subtag.
    # ------------------------------------------------------------------
    op.create_table(
        "notification",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(length=50), nullable=False),
        sa.Column(
            "severity",
            sa.String(length=20),
            nullable=False,
            server_default="INFO",
        ),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body", sa.String(length=1000), nullable=True),
        sa.Column("resource_type", sa.String(length=50), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notification_metadata", sa.JSON(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "locale",
            sa.String(length=35),
            nullable=False,
            server_default=_DEFAULT_LOCALE,
        ),
        sa.CheckConstraint(
            _in_list("type", _NOTIFICATION_TYPES), name="ck_notification_type"
        ),
        sa.CheckConstraint(
            _in_list("severity", _SEVERITIES), name="ck_notification_severity"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
        # The core idempotency guarantee. Scoped by tenant so two tenants may
        # legitimately carry the same key for their own separate occurrences.
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_notification_tenant_idempotency_key"
        ),
    )
    op.create_index("ix_notification_tenant_id", "notification", ["tenant_id"])
    # "All notifications about this resource" — audit/debug access path.
    op.create_index(
        "ix_notification_tenant_resource",
        "notification",
        ["tenant_id", "resource_type", "resource_id"],
    )
    # Type-filtered, time-ordered tenant-wide queries.
    op.create_index(
        "ix_notification_tenant_type_created_at",
        "notification",
        ["tenant_id", "type", "created_at"],
    )

    # ------------------------------------------------------------------
    # 9. notification_recipient — per-user inbox and read state
    #    (Phase 3 Block B, ex-v1_4_0)
    #
    # No `delivered_at` column: for an in-app notification "delivered" is
    # exactly "the row exists", which `created_at` already records.
    # `created_at` is denormalized from the parent notification so the inbox
    # list query never joins for its sort key.
    # ------------------------------------------------------------------
    op.create_table(
        "notification_recipient",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notification_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="UNREAD",
        ),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            _in_list("status", _RECIPIENT_STATUSES),
            name="ck_notification_recipient_status",
        ),
        # A READ row must carry the timestamp that says when. Enforced in the
        # database so "marked read with no read_at" is unrepresentable, not
        # merely unlikely.
        sa.CheckConstraint(
            "status <> 'READ' OR read_at IS NOT NULL",
            name="ck_notification_recipient_read_requires_timestamp",
        ),
        sa.ForeignKeyConstraint(["notification_id"], ["notification.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
        # Recipient-level idempotency: a user is a recipient of a given
        # notification at most once.
        sa.UniqueConstraint(
            "notification_id", "user_id", name="uq_notification_recipient_notification_user"
        ),
    )
    op.create_index(
        "ix_notification_recipient_notification_id",
        "notification_recipient",
        ["notification_id"],
    )
    # Unread-count query: COUNT(*) WHERE tenant_id/user_id/status.
    op.create_index(
        "ix_notification_recipient_inbox_status",
        "notification_recipient",
        ["tenant_id", "user_id", "status"],
    )
    # Inbox list query: WHERE tenant_id/user_id ORDER BY created_at DESC.
    op.create_index(
        "ix_notification_recipient_inbox_created_at",
        "notification_recipient",
        ["tenant_id", "user_id", "created_at"],
    )

    # ------------------------------------------------------------------
    # 10. notification_delivery — external-channel lifecycle
    #     (Phase 3 Block B, ex-v1_4_0, with Block D's final uniqueness model,
    #     ex-v1_5_0)
    #
    # The table is created directly in its final shape. Block B's
    # `UNIQUE (notification_id, channel, recipient_address)` is deliberately
    # NOT created: it assumed one address belongs to one person, so two staff
    # sharing a mailbox — a shared recepcion@, a technician covering a
    # colleague — collapsed into one delivery and the second person silently
    # received nothing. Block D replaced it with two partial unique indexes
    # that split the table by whether the recipient has an account, so every
    # row is covered by exactly one and neither overlaps the other:
    #
    #   - account-backed recipients are keyed on the user, so a shared mailbox
    #     is not a shared delivery, and one user still cannot receive two
    #     deliveries for one event even under two different addresses;
    #   - account-less recipients (a requesting physician resolved straight to
    #     an address) keep the original address guarantee verbatim, because
    #     they have no user id to key on. A plain unique constraint on the
    #     nullable recipient_user_id would reintroduce the NULLs-compare-
    #     distinct hole for exactly those rows.
    #
    # Both indexes are inferrable by `INSERT ... ON CONFLICT (…) WHERE …`, so
    # the duplicate defence stays in the database, not in application logic.
    # ------------------------------------------------------------------
    op.create_table(
        "notification_delivery",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notification_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recipient_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("recipient_address", sa.String(length=320), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("error_code", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            _in_list("channel", _CHANNELS), name="ck_notification_delivery_channel"
        ),
        sa.CheckConstraint(
            _in_list("status", _DELIVERY_STATUSES),
            name="ck_notification_delivery_status",
        ),
        sa.CheckConstraint(
            "attempts >= 0", name="ck_notification_delivery_attempts_non_negative"
        ),
        sa.ForeignKeyConstraint(["notification_id"], ["notification.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["recipient_user_id"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_notification_delivery_notification_id",
        "notification_delivery",
        ["notification_id"],
    )
    op.create_index(
        "ix_notification_delivery_tenant_id", "notification_delivery", ["tenant_id"]
    )
    # The delivery poller's primary query:
    # WHERE status = 'PENDING' AND next_attempt_at <= now().
    op.create_index(
        "ix_notification_delivery_poller",
        "notification_delivery",
        ["status", "next_attempt_at"],
    )
    # Account-backed recipients: one delivery per (event, channel, user).
    op.create_index(
        "uq_notification_delivery_recipient_user",
        "notification_delivery",
        ["notification_id", "channel", "recipient_user_id"],
        unique=True,
        postgresql_where=sa.text("recipient_user_id IS NOT NULL"),
    )
    # Account-less recipients: the original address guarantee, unchanged.
    op.create_index(
        "uq_notification_delivery_recipient_address",
        "notification_delivery",
        ["notification_id", "channel", "recipient_address"],
        unique=True,
        postgresql_where=sa.text("recipient_user_id IS NULL"),
    )

    # ------------------------------------------------------------------
    # 11. notification_preference — per-user, per-type override
    #     (Phase 3 Block B, ex-v1_4_0)
    # ------------------------------------------------------------------
    op.create_table(
        "notification_preference",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notification_type", sa.String(length=50), nullable=False),
        sa.Column(
            "in_app_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "email_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            _in_list("notification_type", _NOTIFICATION_TYPES),
            name="ck_notification_preference_type",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "notification_type", name="uq_notification_preference_user_type"
        ),
    )
    op.create_index(
        "ix_notification_preference_tenant_id", "notification_preference", ["tenant_id"]
    )
    op.create_index(
        "ix_notification_preference_user_id", "notification_preference", ["user_id"]
    )
    # No preference row is seeded. Absence of a row means "use the default".


def downgrade() -> None:
    """Reverse the full 1.3 delta in reverse dependency order.

    Data loss is expected and confined to columns/tables this release
    introduced: any V2 report metadata, PDF integrity metadata, letterhead
    definitions, publish claims and the entire notification history are
    dropped. Preexisting (pre-1.3) columns and rows are untouched, and no S3
    object is deleted — a downgraded deployment simply loses the ability to
    read that metadata until it upgrades again.

    Order is the exact reverse of `upgrade()`, and it is load-bearing rather
    than cosmetic. The notification tables reference each other
    (`notification_recipient` and `notification_delivery` both point at
    `notification`), so `notification` must be dropped last of the four;
    they also reference `tenant` and `app_user`, which this revision does not
    own and must therefore outlive them. Dropping the four first also means
    the letterhead and template-version tables below are dropped with no
    notification row still pointing anywhere near them.

    Every drop is unconditional DDL, so it works on a populated database, not
    only on an empty one: `DROP TABLE` removes the rows with the table, and
    the dependency order means no drop is ever refused by a foreign key.
    """
    # 11 → 8. Notification domain (Phase 3 Blocks B/D/F).
    #
    # preference → delivery → recipient → notification: the two tables that
    # reference `notification` go first, and `notification_preference`, which
    # references neither, goes ahead of both.
    op.drop_index(
        "ix_notification_preference_user_id", table_name="notification_preference"
    )
    op.drop_index(
        "ix_notification_preference_tenant_id", table_name="notification_preference"
    )
    op.drop_table("notification_preference")

    op.drop_index(
        "uq_notification_delivery_recipient_address",
        table_name="notification_delivery",
    )
    op.drop_index(
        "uq_notification_delivery_recipient_user", table_name="notification_delivery"
    )
    op.drop_index("ix_notification_delivery_poller", table_name="notification_delivery")
    op.drop_index(
        "ix_notification_delivery_tenant_id", table_name="notification_delivery"
    )
    op.drop_index(
        "ix_notification_delivery_notification_id", table_name="notification_delivery"
    )
    op.drop_table("notification_delivery")

    op.drop_index(
        "ix_notification_recipient_inbox_created_at",
        table_name="notification_recipient",
    )
    op.drop_index(
        "ix_notification_recipient_inbox_status", table_name="notification_recipient"
    )
    op.drop_index(
        "ix_notification_recipient_notification_id",
        table_name="notification_recipient",
    )
    op.drop_table("notification_recipient")

    op.drop_index("ix_notification_tenant_type_created_at", table_name="notification")
    op.drop_index("ix_notification_tenant_resource", table_name="notification")
    op.drop_index("ix_notification_tenant_id", table_name="notification")
    # `locale` needs no separate drop — it is a column of this table.
    op.drop_table("notification")

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
