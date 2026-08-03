"""v1.6.0 - storage_object.tenant_id (nullable ownership tag)

Revision ID: v1_6_0
Revises: v1_5_0
Create Date: 2026-07-29

Céluma 1.3 Phase 2, Block C, Story C1. Purely additive:

  - `tenant_id` (nullable FK -> tenant.id): NULL for every existing row and
    for every object created by flows that don't set it (sample images,
    report/PDF JSON bodies, user signatures — those already have their
    tenant scoping enforced indirectly through a parent entity: Sample,
    Report, AppUser).
  - Only used going forward to scope objects that are looked up directly by
    id with no parent entity to check ownership through — specifically,
    `ReportRenderingSnapshotV2.presentation.header.logo_storage_id`. See
    report-resource-resolution-contract.md for the full rationale: without
    this column, `create_template_version` could only check that a
    `logo_storage_id` *exists*, not that it belongs to the publishing
    tenant, which would let one tenant's admin reference another tenant's
    private storage object.

No existing `storage_object` row is modified by this migration — no
backfill, no inferred tenant from bucket/key.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "v1_6_0"
down_revision: Union[str, Sequence[str], None] = "v1_5_0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_index("ix_storage_object_tenant_id", table_name="storage_object")
    op.drop_constraint("storage_object_tenant_id_fkey", "storage_object", type_="foreignkey")
    op.drop_column("storage_object", "tenant_id")
