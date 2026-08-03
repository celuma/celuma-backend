"""v1.7.0 - report_version PDF artifact fields (generation status, hash, integrity metadata)

Revision ID: v1_7_0
Revises: v1_6_0
Create Date: 2026-07-31

Céluma 1.3 Phase 2, Block E, Story E1/E2. Purely additive:

  - `pdf_generation_status` (nullable string, CHECK IN ('GENERATING','READY',
    'FAILED')): NULL means "never attempted" for every existing row — no
    status is invented for historical PDFs that may already sit behind
    `pdf_storage_id` from the old ad-hoc upload endpoints. Only rows that go
    through `ReportPdfGenerationService` from this block onward ever get a
    non-NULL value.
  - `pdf_generation_started_at`: set when a generation attempt begins, used
    to detect an orphaned `GENERATING` row after a crash/timeout and allow a
    safe retry.
  - `pdf_generated_at`: set only when the attempt reaches `READY`.
  - `pdf_sha256`, `pdf_size_bytes`, `pdf_page_count`: integrity/audit
    metadata computed by the backend from the actual uploaded bytes — never
    accepted from the client.
  - `pdf_generator_version`: free-form identifier of the generator build
    (e.g. "playwright/1.62.0+chromium-131"), analogous in spirit to the
    existing `generated_by_renderer_version` but specific to the PDF
    artifact rather than the JSON content.
  - `pdf_error_code` / `pdf_error_message`: sanitized, technical-only
    failure details (never clinical content) for the most recent failed
    attempt, cleared on the next successful attempt.

A CHECK constraint enforces that a row can only be marked `READY` if it
actually carries a storage pointer and full integrity metadata — this is the
DB-level backstop for the block's core invariant (no `READY` without a real,
hashed, persisted artifact).

No existing `report_version` row is modified by this migration — no
backfill, no inferred status, no computed hash for PDFs that may already
exist via the old manual upload endpoints.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "v1_7_0"
down_revision: Union[str, Sequence[str], None] = "v1_6_0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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


def downgrade() -> None:
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
