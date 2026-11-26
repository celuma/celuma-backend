"""v1.5.0 - Optional numbering per study type and sample type (#64); catalog type removal (#94); order conversation attachments and notifications (#96, #98)

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

**§3 — order conversation (#96, #98).** A new, EMPTY table
`order_comment_attachment` (one row per image of a conversation message: tenant,
branch, order, comment, position 0–3, processed and thumbnail `storage_object`
ids, pixel size, author, timestamp; unique `(comment_id, position)`, index on
`(tenant_id, order_id)`), and the notification type `ORDER_COMMENT_ADDED`:
`ck_notification_type` and `ck_notification_preference_type` are recreated with
the eleven 1.4 values plus this one (a constraint swap; no row is touched).
Existing comments get no attachment and no notification is back-filled.
Specification: `celuma-engineering/specs/laboratory/order-conversation.md`;
decision record: `celuma-engineering/adr/0026-order-conversation-attachments.md`.

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
the pre-upgrade snapshot instead. §3's downgrade REFUSES while any conversation
attachment or `ORDER_COMMENT_ADDED` notification exists (images in storage and
inbox history that 1.4 cannot represent; restore the snapshot); otherwise it
deletes `ORDER_COMMENT_ADDED` preference rows (user settings only), restores
both constraints to the 1.4 set and drops the table. §3's locks: the brief
ACCESS EXCLUSIVE of the two constraint swaps on `notification` and
`notification_preference` (the new CHECK validates by a scan of each), and FK
checks against `tenant`, `branch`, `order`, `order_comment`, `storage_object`
and `app_user` for the new, empty table. Otherwise it restores the full constraint and
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

#: §3 (#96): images of order conversation messages.
ATTACHMENT_TABLE = "order_comment_attachment"
#: §3 (#98): the notification types `v1_4_0` admits (frozen copy) and the one this revision adds.
V1_4_NOTIFICATION_TYPES = (
    "REPORT_SUBMITTED", "REPORT_PDF_READY", "REPORT_PUBLISHED", "REPORT_RETRACTED",
    "ASSIGNMENT_ADDED", "SAMPLE_STATUS_CHANGED", "STORAGE_USAGE_APPROACHING",
    "STORAGE_LIMIT_REACHED", "USER_LIMIT_APPROACHING", "USER_LIMIT_REACHED",
    "REPORT_REPUBLISHED",
)
COMMENT_NOTIFICATION_TYPE = "ORDER_COMMENT_ADDED"


def _notification_type_checks(types) -> None:
    """Recreate both notification-domain CHECK constraints over `types` (same swap as `v1_4_0` §6c)."""
    rendered = ", ".join(f"'{value}'" for value in types)
    for table, column, name in (
        ("notification", "type", "ck_notification_type"),
        ("notification_preference", "notification_type", "ck_notification_preference_type"),
    ):
        op.execute(f"ALTER TABLE public.{table} DROP CONSTRAINT {name}")
        op.execute(f"ALTER TABLE public.{table} ADD CONSTRAINT {name} CHECK ({column} IN ({rendered}))")


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
    # §3 (#96, #98) — order conversation attachments and the comment notification type.
    op.execute(
        f"""
        CREATE TABLE public.{ATTACHMENT_TABLE} (
            id uuid NOT NULL,
            tenant_id uuid NOT NULL REFERENCES public.tenant(id),
            branch_id uuid NOT NULL REFERENCES public.branch(id),
            order_id uuid NOT NULL REFERENCES public."order"(id),
            comment_id uuid NOT NULL REFERENCES public.order_comment(id),
            position integer NOT NULL,
            storage_id uuid NOT NULL REFERENCES public.storage_object(id),
            thumbnail_storage_id uuid NOT NULL REFERENCES public.storage_object(id),
            width integer NOT NULL,
            height integer NOT NULL,
            created_by uuid NOT NULL REFERENCES public.app_user(id),
            created_at timestamp without time zone NOT NULL,
            CONSTRAINT pk_{ATTACHMENT_TABLE} PRIMARY KEY (id),
            CONSTRAINT uq_{ATTACHMENT_TABLE}_position UNIQUE (comment_id, position),
            CONSTRAINT ck_{ATTACHMENT_TABLE}_position CHECK (position BETWEEN 0 AND 3),
            CONSTRAINT ck_{ATTACHMENT_TABLE}_size CHECK (width > 0 AND height > 0)
        )
        """
    )
    op.execute(f"CREATE INDEX ix_{ATTACHMENT_TABLE}_tenant_order ON public.{ATTACHMENT_TABLE} (tenant_id, order_id)")
    _notification_type_checks(V1_4_NOTIFICATION_TYPES + (COMMENT_NOTIFICATION_TYPE,))


def downgrade() -> None:
    conn = op.get_bind()
    # §3 first (reverse order). Refused while it would destroy history 1.4 cannot hold.
    attachments = conn.exec_driver_sql(f"SELECT count(*) FROM public.{ATTACHMENT_TABLE}").scalar()
    comment_notifications = conn.exec_driver_sql(
        f"SELECT count(*) FROM public.notification WHERE type = '{COMMENT_NOTIFICATION_TYPE}'"
    ).scalar()
    if attachments or comment_notifications:
        raise RuntimeError(
            f"v1_5_0 downgrade refused: {attachments} conversation attachment(s) and {comment_notifications} "
            f"{COMMENT_NOTIFICATION_TYPE} notification(s) exist. The pre-1.5 schema cannot represent them; restore "
            "the pre-upgrade snapshot instead (nothing was changed)."
        )
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
    op.execute(
        f"DELETE FROM public.notification_preference WHERE notification_type = '{COMMENT_NOTIFICATION_TYPE}'"
    )
    _notification_type_checks(V1_4_NOTIFICATION_TYPES)
    op.execute(f"DROP TABLE public.{ATTACHMENT_TABLE}")
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
