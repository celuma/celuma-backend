"""v1.4.1 - The order's explicitly selected report template (Céluma 1.4.1 hotfix)

Revision ID: v1_4_1
Revises: v1_4_0
Create Date: 2026-10-31

Céluma 1.4 shipped with `v1_4_0`, which is now frozen: it is a published
release migration and is never edited. The order-template hotfix needs one
column, so it gets its own linear revision on top, following the standing
contract of one revision per shipped product release (`v1_3_1` was the
precedent for a hotfix release).

Specification: `celuma-engineering/specs/laboratory/order-report-template.md`;
decision record: `celuma-engineering/adr/0022-order-report-template-selection.md`.

**`order.report_template_id`.** A nullable FK to `report_template` (no
`ON DELETE` action, so a referenced template cannot be deleted under an order;
the API answers 409 first) and a plain index for that reference check. It
stores only an EXPLICIT choice made by a user; NULL means "no explicit choice,
use the catalog defaults" (the sample type's template when the order's samples
agree on one, else the study type's). Every existing order gets NULL, which
resolves exactly as 1.4.0 did until a laboratory links templates to its sample
types. Nothing is backfilled or rewritten; no report, snapshot, letterhead or
published artifact is touched.

**Locks.** `ALTER TABLE "order" ADD COLUMN` with no default is a metadata-only
change (brief ACCESS EXCLUSIVE lock, no table rewrite); the FK is added
`NOT VALID` and then validated, so the validation scan (trivial: every value is
NULL) holds only a SHARE UPDATE EXCLUSIVE lock; the index build is a plain
`CREATE INDEX` like `v1_4_0` §1b, inside the drained release window
(`celuma-engineering/runbooks/migrations.md`).

**Downgrade** drops the index, the FK and the column: every explicit choice is
lost and 1.4.0 code resolves the study type's template again. 1.4.0 code never
reads the column, but a 1.4.0 image does NOT start on a `v1_4_1` database: its
`start.sh` head check cannot locate this revision and exits. An application
rollback to 1.4.0 therefore needs either this downgrade (run as a controlled
task with the 1.4.1 image, accepting the loss of the explicit choices) or a
restore of the pre-upgrade snapshot. Never stamp.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "v1_4_1"
down_revision: Union[str, Sequence[str], None] = "v1_4_0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


COLUMN = "report_template_id"
FK_NAME = "fk_order_report_template"
INDEX_NAME = "ix_order_report_template_id"


def upgrade() -> None:
    op.execute(f'ALTER TABLE public."order" ADD COLUMN {COLUMN} uuid')
    op.execute(
        f'ALTER TABLE public."order" ADD CONSTRAINT {FK_NAME} FOREIGN KEY ({COLUMN}) '
        "REFERENCES public.report_template(id) NOT VALID"
    )
    op.execute(f'ALTER TABLE public."order" VALIDATE CONSTRAINT {FK_NAME}')
    op.execute(f'CREATE INDEX {INDEX_NAME} ON public."order" USING btree ({COLUMN})')


def downgrade() -> None:
    op.execute(f"DROP INDEX public.{INDEX_NAME}")
    op.execute(f'ALTER TABLE public."order" DROP CONSTRAINT {FK_NAME}')
    op.execute(f'ALTER TABLE public."order" DROP COLUMN {COLUMN}')
