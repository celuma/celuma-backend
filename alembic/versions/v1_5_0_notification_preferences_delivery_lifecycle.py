"""v1.5.0 - Notification delivery uniqueness by recipient (Phase 3, Block D)

Revision ID: v1_5_0
Revises: v1_4_0
Create Date: 2026-08-06

The problem this fixes
----------------------
`v1_4_0` gave `notification_delivery` the uniqueness guarantee Block A
specified:

    UNIQUE (notification_id, channel, recipient_address)

That constraint does the job it was designed for — it stops the same message
being sent twice to the same mailbox for the same event — and Block B's
decision to make `recipient_address` NOT NULL is what makes it real, since
NULLs compare distinct in PostgreSQL and a nullable column would have let
duplicates through the very constraint meant to prevent them.

But it also encodes an assumption that is not true of a laboratory: that one
address belongs to one person. Two staff users legitimately sharing a mailbox
— a shared `recepcion@`, a small practice where two pathologists read the same
inbox, a technician covering a colleague's address — are two distinct
recipients of a notification. Under the old constraint the second user's
delivery row is silently swallowed by `ON CONFLICT DO NOTHING`, and Block D
would have shipped a rule of "the second person to share an address never
gets email, and nothing anywhere records that they didn't".

The principle this revision encodes instead:

    Delivery intent belongs to the notification recipient, not to an address.

Why not simply UNIQUE (notification_id, channel, recipient_user_id)
-------------------------------------------------------------------
Because `recipient_user_id` is nullable, and deliberately so: Block A's
recipient matrix routes a requesting physician who has no `AppUser` account
to their address directly, which Block E will need. Swapping one constraint
for the other would reintroduce, for exactly those rows, the NULLs-compare-
distinct hole that Block B closed for the address column — the account-less
recipients being the ones a bug is most likely to duplicate.

The same objection applies to a combined
`(notification_id, channel, recipient_user_id, recipient_address)`: a NULL in
any column of a multi-column unique constraint makes the whole row distinct.

The shape that actually holds
-----------------------------
Two **partial** unique indexes, splitting the table by whether the recipient
has an account, so every row is covered by exactly one of them:

    uq_notification_delivery_recipient_user
        (notification_id, channel, recipient_user_id)
        WHERE recipient_user_id IS NOT NULL
        -> one delivery per (event, channel, user). Two users sharing a
           mailbox each get their own row; one user cannot get two.

    uq_notification_delivery_recipient_address
        (notification_id, channel, recipient_address)
        WHERE recipient_user_id IS NULL
        -> the original guarantee, retained verbatim for account-less
           recipients, who have no user id to key on.

No column is added, dropped or altered. Both indexes are inferrable by
`INSERT ... ON CONFLICT (…) WHERE …`, so the duplicate defence stays in the
database rather than moving into application logic.

Data
----
No backfill, and none possible: `notification_delivery` is written by nothing
before this revision (Block B created the table and left it empty; Block C is
frontend-only), so every environment reaches this migration with zero rows.
No preference row is seeded either — absence of a row is what "use the
default" means, and Block D's API depends on that staying true.

Downgrade restores the original constraint exactly. It can only fail if rows
exist that the old, stricter shape forbids — i.e. two users who share a
mailbox were both notified of one event, the case this revision exists to
support. That is the correct behaviour: refusing to downgrade is better than
choosing which of two people's delivery records to delete.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "v1_5_0"
down_revision: Union[str, Sequence[str], None] = "v1_4_0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_OLD_CONSTRAINT = "uq_notification_delivery_notification_channel_address"
_USER_INDEX = "uq_notification_delivery_recipient_user"
_ADDRESS_INDEX = "uq_notification_delivery_recipient_address"


def upgrade() -> None:
    op.drop_constraint(
        _OLD_CONSTRAINT, "notification_delivery", type_="unique"
    )

    # Account-backed recipients: keyed on the user, so a shared mailbox is
    # not a shared delivery.
    op.create_index(
        _USER_INDEX,
        "notification_delivery",
        ["notification_id", "channel", "recipient_user_id"],
        unique=True,
        postgresql_where=sa.text("recipient_user_id IS NOT NULL"),
    )

    # Account-less recipients (a physician resolved straight to an address,
    # Block E): the original address guarantee, unchanged.
    op.create_index(
        _ADDRESS_INDEX,
        "notification_delivery",
        ["notification_id", "channel", "recipient_address"],
        unique=True,
        postgresql_where=sa.text("recipient_user_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(_ADDRESS_INDEX, table_name="notification_delivery")
    op.drop_index(_USER_INDEX, table_name="notification_delivery")
    op.create_unique_constraint(
        _OLD_CONSTRAINT,
        "notification_delivery",
        ["notification_id", "channel", "recipient_address"],
    )
