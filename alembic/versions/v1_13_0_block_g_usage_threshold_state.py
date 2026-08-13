"""v1.13.0 - Céluma 1.3, Phase 4, Block G: durable usage-threshold state

Revision ID: v1_13_0
Revises: v1_12_0
Create Date: 2026-08-12

Two changes:

  1. The `tenant_usage_threshold_state` table, one row per
     `(tenant_id, resource)`, holding where a tenant currently sits relative
     to its storage/user limit. See
     `app/models/tenant_usage_threshold_state.py` for why threshold state is
     its own table rather than columns on `tenant_usage`.

  2. Widening `ck_notification_type` and `ck_notification_preference_type` to
     admit the four usage-threshold notification types.

The second is exactly the cost the notification domain chose when it stored
enums as `VARCHAR` + `CHECK` instead of a native PostgreSQL `ENUM` (see
`app/models/notification.py`'s module docstring): adding a value is a
constraint change, which runs inside a transaction and reverts cleanly,
rather than `ALTER TYPE ... ADD VALUE`, which historically could not do
either. Both constraints are dropped and recreated with the full list — an
`ALTER TABLE ... DROP CONSTRAINT` + `ADD CONSTRAINT` pair, not a
`NOT VALID` shortcut, because the existing rows all carry Phase 3 values and
therefore already satisfy the wider list.

`notification_preference` is widened alongside `notification` and not left
for later: the preference API accepts any `NotificationType`, so a user
switching email off for `STORAGE_LIMIT_REACHED` writes a row whose value the
narrower constraint would reject.

**This revision creates no notification, and cannot.**
-------------------------------------------------------
Schema only. No backfill, no seeded rows, no `NotificationService` import —
nothing here reads `tenant_usage`, `tenant_limits` or `app_user` at all. That
is a deliberate choice with a concrete failure mode behind it:

  - The production database has 133 tenants. A baseline pass inside the
    migration that decided "this tenant is already at 104%, record REACHED"
    would either (a) record the state *without* notifying, permanently
    swallowing the first real crossing for every tenant already above a
    threshold — the exact "already over the limit at deployment stays silent
    forever" outcome the first-evaluation policy exists to prevent; or
    (b) notify from inside a schema migration, which would fan a bulk mail-out
    across every tenant at deploy time, inside a DDL transaction, with no
    request context and no way to abort halfway.
  - An empty table means every `(tenant, resource)` pair is "never
    evaluated", and the first runtime evaluation applies the documented
    first-evaluation semantics (one notification for the current highest
    meaningful state, never APPROACHING *and* REACHED). That is the same code
    path a brand-new tenant takes, so there is exactly one behaviour to
    reason about and to test.

Constraint choices
------------------
`UNIQUE (tenant_id, resource)` is the load-bearing one: it is what makes
"one semantic threshold transition -> one notification" enforceable in the
database rather than in application logic. The service's upsert-then-lock
sequence depends on it — `INSERT ... ON CONFLICT DO NOTHING` infers this
constraint, so two concurrent first evaluations for the same tenant serialize
on the index instead of both inserting a row.

`resource` and `state` are `VARCHAR` + `CHECK`, not native enums — the
convention `notification.py`'s module docstring sets out and every Céluma 1.3
table follows.

`ck_tenant_usage_threshold_state_unmonitored_has_no_values` encodes the
domain rule that `UNMONITORED` means "not evaluable", so it cannot be carrying
the numbers a real evaluation would have produced. `last_value`/`last_limit`
are otherwise nullable independently: a `NULL` limit with a non-null value is
unreachable in practice (no limit => UNMONITORED), and constraining it would
add a rule the service does not need.

No index beyond the unique constraint. Every read this table gets is
`WHERE tenant_id = ? AND resource = ?`, which the unique index already
serves; a tenant-only index would be redundant with its leading column.

No `ON DELETE` on the tenant FK, matching `tenant_usage`, `tenant_limits` and
`tenant_usage_reconciliation`: tenant deletion is refused while history
exists rather than silently cascading.

Downgrade drops the table. That loses remembered threshold state, which is
correct and safe: after a re-upgrade every pair is "never evaluated" again
and the next evaluation re-establishes the current state under
first-evaluation semantics. The worst case is one repeated notification for a
tenant that is genuinely above a threshold — never a *missed* one, and never a
wrong number, because the state is always re-derived from live usage and
limits rather than from anything this table remembers.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "v1_13_0"
down_revision: Union[str, Sequence[str], None] = "v1_12_0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_RESOURCES = ("STORAGE", "USERS")
_STATES = ("UNMONITORED", "NORMAL", "APPROACHING", "REACHED")

#: The Phase 3 clinical types, exactly as `v1_3_0` wrote them. Restated here
#: rather than imported from `app.models.notification` on purpose: a migration
#: must describe the schema at *its* point in history, and importing a live
#: enum would make this revision's DDL change every time that enum does —
#: which is how a downgrade stops reproducing the schema it claims to.
_PHASE_3_NOTIFICATION_TYPES = (
    "REPORT_SUBMITTED",
    "REPORT_PDF_READY",
    "REPORT_PUBLISHED",
    "REPORT_RETRACTED",
    "ASSIGNMENT_ADDED",
    "SAMPLE_STATUS_CHANGED",
)

#: The four this revision adds.
_BLOCK_G_NOTIFICATION_TYPES = (
    "STORAGE_USAGE_APPROACHING",
    "STORAGE_LIMIT_REACHED",
    "USER_LIMIT_APPROACHING",
    "USER_LIMIT_REACHED",
)

#: Which CHECK constraint on which table/column carries the type list.
_TYPE_CONSTRAINTS = (
    ("notification", "ck_notification_type", "type"),
    ("notification_preference", "ck_notification_preference_type", "notification_type"),
)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _rewrite_type_constraints(values: tuple[str, ...]) -> None:
    for table, name, column in _TYPE_CONSTRAINTS:
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, _in_list(column, values))


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. The durable threshold state
    # ------------------------------------------------------------------
    op.create_table(
        "tenant_usage_threshold_state",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("resource", sa.String(length=20), nullable=False),
        sa.Column(
            "state",
            sa.String(length=20),
            nullable=False,
            server_default="UNMONITORED",
        ),
        sa.Column("last_value", sa.BigInteger(), nullable=True),
        sa.Column("last_limit", sa.BigInteger(), nullable=True),
        sa.Column(
            "transition_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("last_transition_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            f"resource IN ({', '.join(repr(r) for r in _RESOURCES)})",
            name="ck_tenant_usage_threshold_state_resource",
        ),
        sa.CheckConstraint(
            f"state IN ({', '.join(repr(s) for s in _STATES)})",
            name="ck_tenant_usage_threshold_state_state",
        ),
        # "Not evaluable" must not be carrying the numbers of an evaluation.
        sa.CheckConstraint(
            "state <> 'UNMONITORED' OR (last_value IS NULL AND last_limit IS NULL)",
            name="ck_tenant_usage_threshold_state_unmonitored_has_no_values",
        ),
        sa.CheckConstraint(
            "last_value IS NULL OR last_value >= 0",
            name="ck_tenant_usage_threshold_state_value_non_negative",
        ),
        sa.CheckConstraint(
            "last_limit IS NULL OR last_limit > 0",
            name="ck_tenant_usage_threshold_state_limit_positive",
        ),
        sa.CheckConstraint(
            "transition_count >= 0",
            name="ck_tenant_usage_threshold_state_transition_count_non_negative",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "resource",
            name="uq_tenant_usage_threshold_state_tenant_resource",
        ),
    )

    # ------------------------------------------------------------------
    # 2. Admit the four usage-threshold notification types
    # ------------------------------------------------------------------
    _rewrite_type_constraints(
        _PHASE_3_NOTIFICATION_TYPES + _BLOCK_G_NOTIFICATION_TYPES
    )


def downgrade() -> None:
    # Narrow the constraints first, and delete the rows they would now reject.
    #
    # A plain narrowing would fail on any tenant that has actually been
    # notified — the constraint is validated against existing rows — so the
    # downgrade has to decide what happens to that history. It deletes it,
    # scoped to precisely the four types this revision introduced:
    #
    #   - these rows describe a feature the downgraded application does not
    #     have. Left behind, they would be inbox entries whose type the
    #     frontend renders under its unknown-type fallback and whose deep link
    #     resolves to nothing;
    #   - nothing clinical is lost. A usage-threshold notification carries no
    #     patient, sample or report reference — it is an administrative
    #     statement about a number that is still fully derivable from
    #     `tenant_usage` and `tenant_limits`;
    #   - the alternative, leaving them and skipping the constraint, would
    #     mean a downgraded database no longer matches the schema `v1_12_0`
    #     produces, which is the one property a downgrade exists to restore.
    #
    # Deletion order follows the foreign keys: deliveries and recipients
    # reference the notification, which is why none of these FKs carries a
    # cascade.
    block_g = ", ".join(repr(value) for value in _BLOCK_G_NOTIFICATION_TYPES)
    op.execute(
        f"DELETE FROM notification_delivery WHERE notification_id IN "
        f"(SELECT id FROM notification WHERE type IN ({block_g}))"
    )
    op.execute(
        f"DELETE FROM notification_recipient WHERE notification_id IN "
        f"(SELECT id FROM notification WHERE type IN ({block_g}))"
    )
    op.execute(f"DELETE FROM notification WHERE type IN ({block_g})")
    op.execute(f"DELETE FROM notification_preference WHERE notification_type IN ({block_g})")

    _rewrite_type_constraints(_PHASE_3_NOTIFICATION_TYPES)

    op.drop_table("tenant_usage_threshold_state")
