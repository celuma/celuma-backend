"""v1.6.0 - Record the locale a notification's frozen copy was rendered in
(Phase 3, Block F)

Revision ID: v1_6_0
Revises: v1_5_0
Create Date: 2026-08-07

One additive column::

    notification.locale  VARCHAR(35)  NOT NULL  DEFAULT 'es-MX'

Nothing else. No preference change, no delivery change, no clinical table, no
backfill of anything but this column, and not one byte of existing
`title`/`body`/`notification_metadata` is rewritten.

Why the column exists at all
----------------------------
Block F evaluated leaving locale implicit (Story F6, Option A). The argument
for implicit is real: `title` and `body` are frozen at creation, so *in-app
display* is already reproducible without knowing the locale, and Céluma 1.3
supports only `es-MX` anyway.

It was rejected because two consumers need the locale itself, not the text it
produced:

1. **The delivery worker renders an independent email.** It does not copy the
   notification's frozen text (deliberately — the email vocabulary is
   strictly narrower, see email-template-contract.md §1); it re-renders from
   `template_key` + `template_params`. That render needs a locale, and taking
   "whatever the default is at delivery time" would mean a notification
   created before a default changed gets an email in a different language from
   the in-app copy it accompanies. Reading it off the row makes the two agree
   by construction.

2. **Audit cannot reconstruct it.** Once a second locale exists, "which locale
   did this user actually read this in" has no answer derivable from the
   stored text — matching Spanish strings to infer a locale is not an audit
   trail. The fact is available for free at creation time and impossible to
   recover afterwards.

Why the backfill to 'es-MX' is provable, not assumed
-----------------------------------------------------
Every notification row that can exist before this revision was written by
`NotificationService.notify()`, which is the only writer — there is no other
insert path in the codebase and no notification-creation endpoint. Between
Blocks B and E that service rendered from a single hardcoded Spanish registry
(`app/services/notification_templates.py`), which had no locale parameter, no
locale argument on `NotificationCommand`, and exactly one copy per type. A
row in some other language is therefore not merely unlikely, it was not
expressible.

The server default does the backfill: `ALTER TABLE ... ADD COLUMN ... NOT NULL
DEFAULT` fills every existing row with `'es-MX'` in one statement, so there is
no separate `UPDATE` and no window in which the column is nullable.

The default is kept on the column after the fact, rather than dropped. A
default that matches the only value the application writes is not a trap; it
is what makes a hand-inserted debugging row consistent with the rest of the
table instead of failing a NOT NULL check.

VARCHAR(35), not VARCHAR(5): `es-MX` is five characters, but a BCP-47
identifier with a script subtag (`zh-Hant-TW`) is not, and widening a column
later is a migration nobody wants to run for a naming reason. 35 is the bound
`app/services/locale.py` enforces on input.

Reversibility
-------------
`downgrade()` drops the column. That loses the recorded locale, which is
acceptable in exactly the situation a downgrade describes: reverting to a
schema whose application had no concept of locale and rendered everything in
`es-MX` regardless. No other data is touched, and the frozen `title`/`body`
every user actually saw are not among the things this column could have
changed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "v1_6_0"
down_revision: Union[str, Sequence[str], None] = "v1_5_0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_DEFAULT_LOCALE = "es-MX"


def upgrade() -> None:
    op.add_column(
        "notification",
        sa.Column(
            "locale",
            sa.String(length=35),
            nullable=False,
            server_default=_DEFAULT_LOCALE,
        ),
    )


def downgrade() -> None:
    op.drop_column("notification", "locale")
