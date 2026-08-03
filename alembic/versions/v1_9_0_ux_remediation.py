"""v1.9.0 - post-phase-2 UX remediation: preferred_letterhead_id + publish lock

Revision ID: v1_9_0
Revises: v1_8_0
Create Date: 2026-08-01

Second post-Phase-2 remediation (UX). Purely additive:
  - `report_template.preferred_letterhead_id`: nullable FK to
    `report_letterhead.id` (the logical letterhead, NOT a specific version).
    Sibling of the existing `preferred_letterhead_version_id` (kept, now
    read-only for old rows — see report-template-simplification-contract.md).
    No `ondelete` specified, mirroring the existing sibling FK exactly.
  - `report_version.publish_started_at` / `publish_started_by`: nullable
    claim columns preventing double sign-and-publish (double Chromium,
    double firma), mirroring the existing
    `pdf_generation_started_at`/staleness pattern used by
    `ReportPdfGenerationService`. See signed-pdf-publication-workflow.md.

No existing data is modified, no backfill runs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "v1_9_0"
down_revision: Union[str, None] = "v1_8_0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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
    op.drop_constraint(
        "fk_report_version_publish_started_by", "report_version", type_="foreignkey"
    )
    op.drop_column("report_version", "publish_started_by")
    op.drop_column("report_version", "publish_started_at")

    op.drop_constraint(
        "fk_report_template_preferred_letterhead_id", "report_template", type_="foreignkey"
    )
    op.drop_column("report_template", "preferred_letterhead_id")
