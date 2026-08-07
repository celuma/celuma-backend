"""v1.4.0 - Céluma Notifications core (Phase 3, Block B)

Revision ID: v1_4_0
Revises: v1_3_0
Create Date: 2026-08-05

Creates the four notification-domain tables approved in Block A
(docs/celuma-1.3/phase-3-block-a/notification-domain-model-proposal.md §2):

  1. `notification`             shared, immutable event + frozen content
  2. `notification_recipient`   per-user inbox row and read state
  3. `notification_delivery`    external-channel delivery lifecycle
  4. `notification_preference`  per-user, per-type channel override

`notification_delivery` and `notification_preference` are created here even
though Block B neither writes nor reads them: they are part of the approved
core model, and landing all four at once means Blocks D/E add behaviour
without another schema migration.

Design notes
------------
  - Every enum is a plain VARCHAR + CHECK constraint, never a native
    PostgreSQL ENUM, following the convention v1_3_0 established for
    `report_template_version.status`/`report_letterhead_version.status`:
    adding or retiring a value is a constraint change instead of `ALTER TYPE
    ... ADD VALUE`, which cannot run inside a transaction on older PostgreSQL
    and is awkward to revert. This matters specifically here because
    `notification_channel` ships with EMAIL only — PUSH/SMS are deliberately
    absent until they are real.
  - `notification.resource_type` carries NO check constraint and
    `resource_id` NO foreign key: the pair is polymorphic, exactly like
    `audit_log.entity_type`/`entity_id`. Allowed resource types are enforced
    at the service/API boundary instead, so a new resource type never needs a
    migration.
  - `UNIQUE (tenant_id, idempotency_key)` on `notification` is the
    load-bearing idempotency guarantee. It is a real database constraint, not
    an application check, so concurrent inserts of the same occurrence are
    serialized by PostgreSQL itself (idempotency strategy §2/§6 example 5).
  - `notification_delivery.recipient_address` is NOT NULL. EMAIL is the only
    channel in Phase 3 and an email delivery without an address is not a
    meaningful row; more importantly, a nullable column would silently defeat
    `UNIQUE (notification_id, channel, recipient_address)`, since NULLs
    compare distinct in PostgreSQL and duplicate rows would slip through the
    very constraint meant to prevent double sends.
  - `notification_recipient` has NO `delivered_at` column. Block A proposed
    one, but for an in-app notification "delivered" is exactly "the row
    exists", which `created_at` already records. See
    docs/celuma-1.3/phase-3-block-b/phase-3-block-b-architecture-decision.md.
  - `notification_recipient.created_at` is denormalized from the parent
    notification so the inbox list query never joins for its sort key. The
    service writes the parent's exact timestamp into it.
  - No FK carries an ON DELETE clause, which defaults to PostgreSQL NO
    ACTION/RESTRICT: deleting a user or tenant that still owns notification
    history is refused rather than silently erasing the audit trail.

This migration is purely additive. It modifies no existing table, runs no
backfill, and creates no notification, recipient, delivery or preference row.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "v1_4_0"
down_revision: Union[str, Sequence[str], None] = "v1_3_0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Kept as literals rather than imported from app.models so the migration
#: stays a frozen historical record: a later edit to the Python enum must not
#: retroactively change what this revision created.
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


def _in_list(column: str, values: Sequence[str]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. notification — shared, immutable event record
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
    # 2. notification_recipient — per-user inbox and read state
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
    # 3. notification_delivery — external-channel lifecycle (Blocks D/E)
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
        # Delivery-level idempotency. Meaningful only because
        # recipient_address is NOT NULL — see the module docstring.
        sa.UniqueConstraint(
            "notification_id",
            "channel",
            "recipient_address",
            name="uq_notification_delivery_notification_channel_address",
        ),
    )
    op.create_index(
        "ix_notification_delivery_notification_id",
        "notification_delivery",
        ["notification_id"],
    )
    op.create_index(
        "ix_notification_delivery_tenant_id", "notification_delivery", ["tenant_id"]
    )
    # The Block E poller's primary query:
    # WHERE status = 'PENDING' AND next_attempt_at <= now().
    op.create_index(
        "ix_notification_delivery_poller",
        "notification_delivery",
        ["status", "next_attempt_at"],
    )

    # ------------------------------------------------------------------
    # 4. notification_preference — per-user override (Block D)
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
    """Drop all four tables in dependency-safe order.

    Nothing outside this revision is touched: no pre-existing table was
    modified on the way up, so there is nothing to restore. Data loss is
    confined to notification history, which did not exist before v1_4_0.
    """
    op.drop_index(
        "ix_notification_preference_user_id", table_name="notification_preference"
    )
    op.drop_index(
        "ix_notification_preference_tenant_id", table_name="notification_preference"
    )
    op.drop_table("notification_preference")

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
    op.drop_table("notification")
