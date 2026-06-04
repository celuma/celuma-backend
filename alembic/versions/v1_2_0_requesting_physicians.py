"""v1.2.0 - Requesting physicians catalog, optional order patient, and pathologist image permissions

Revision ID: v1_2_0
Revises: v1_1_0
Create Date: 2026-05-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "v1_2_0"
down_revision: Union[str, Sequence[str], None] = "v1_1_0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "requesting_physician",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("physician_code", sa.String(length=100), nullable=False),
        sa.Column("first_name", sa.String(length=255), nullable=False),
        sa.Column("last_name", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("specialty", sa.String(length=255), nullable=True),
        sa.Column("professional_license", sa.String(length=100), nullable=True),
        sa.Column("institution", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("address", sa.String(length=500), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.ForeignKeyConstraint(["branch_id"], ["branch.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "physician_code", name="uq_requesting_physician_tenant_code"),
    )
    op.create_index("ix_requesting_physician_tenant_id", "requesting_physician", ["tenant_id"])
    op.create_index("ix_requesting_physician_branch_id", "requesting_physician", ["branch_id"])
    op.create_index("ix_requesting_physician_physician_code", "requesting_physician", ["physician_code"])
    op.create_index("ix_requesting_physician_email", "requesting_physician", ["email"])
    op.create_index("ix_requesting_physician_is_active", "requesting_physician", ["is_active"])

    op.add_column(
        "order",
        sa.Column("requesting_physician_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "lab_order_requesting_physician_id_fkey",
        source_table="order",
        referent_table="requesting_physician",
        local_cols=["requesting_physician_id"],
        remote_cols=["id"],
    )
    op.create_index("ix_order_requesting_physician_id", "order", ["requesting_physician_id"])
    op.alter_column(
        "order",
        "patient_id",
        existing_type=postgresql.UUID(as_uuid=True),
        existing_nullable=False,
        nullable=True,
    )

    # ------------------------------------------------------------------
    # DML: grant lab:upload_images and lab:delete_images to pathologist
    # ------------------------------------------------------------------
    op.execute(
        """
        INSERT INTO public.role_permission (role_id, permission_id)
        SELECT r.id, p.id
        FROM public.role r
        CROSS JOIN public.permission p
        WHERE r.code = 'pathologist'
          AND p.code IN ('lab:upload_images', 'lab:delete_images')
          AND NOT EXISTS (
              SELECT 1
              FROM public.role_permission existing
              WHERE existing.role_id = r.id
                AND existing.permission_id = p.id
          )
        """
    )


def downgrade() -> None:
    # ------------------------------------------------------------------
    # DML: revoke lab:upload_images and lab:delete_images from pathologist
    # ------------------------------------------------------------------
    op.execute(
        """
        DELETE FROM public.role_permission rp
        USING public.role r, public.permission p
        WHERE rp.role_id = r.id
          AND rp.permission_id = p.id
          AND r.code = 'pathologist'
          AND p.code IN ('lab:upload_images', 'lab:delete_images')
        """
    )

    op.alter_column(
        "order",
        "patient_id",
        existing_type=postgresql.UUID(as_uuid=True),
        existing_nullable=True,
        nullable=False,
    )
    op.drop_index("ix_order_requesting_physician_id", table_name="order")
    op.drop_constraint("lab_order_requesting_physician_id_fkey", "order", type_="foreignkey")
    op.drop_column("order", "requesting_physician_id")

    op.drop_index("ix_requesting_physician_is_active", table_name="requesting_physician")
    op.drop_index("ix_requesting_physician_email", table_name="requesting_physician")
    op.drop_index("ix_requesting_physician_physician_code", table_name="requesting_physician")
    op.drop_index("ix_requesting_physician_branch_id", table_name="requesting_physician")
    op.drop_index("ix_requesting_physician_tenant_id", table_name="requesting_physician")
    op.drop_table("requesting_physician")
