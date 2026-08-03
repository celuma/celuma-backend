"""v1.3.0 - tenant.reports_v2_enabled feature flag

Revision ID: v1_3_0
Revises: v1_2_0
Create Date: 2026-07-29

Céluma 1.3 Phase 2, Block A, Story A6. Purely additive: introduces a
tenant-level flag that will gate CREATION of new V2 reports in a later
block. It does not affect how existing reports are rendered (see
report-schema-versioning.md) and does not change report creation in this
block — the flag is not read anywhere yet.

Changes:
  - DDL: add tenant.reports_v2_enabled (boolean, NOT NULL, default false)

server_default=sa.false() means existing rows are backfilled to false in the
same ALTER TABLE statement — no separate UPDATE/backfill pass needed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "v1_3_0"
down_revision: Union[str, Sequence[str], None] = "v1_2_0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenant",
        sa.Column(
            "reports_v2_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("tenant", "reports_v2_enabled")
