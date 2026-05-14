"""v1.1.0 - Digital signature: app_user.signature_storage_id + reviewer role

Revision ID: v1_1_0
Revises: v1_0_0
Create Date: 2026-05-13

Changes:
  - DDL: add app_user.signature_storage_id (FK to storage_object.id, nullable)
  - DML: insert new system role 'reviewer' with permissions
         reports:read, reports:approve, reports:sign, lab:read, lab:manage_reviewers
  - DML: remove permission 'reports:sign' from system role 'pathologist'

Downgrade restores the previous state (re-grants reports:sign to pathologist,
deletes the reviewer role and its permissions, and drops the new column).
"""
from typing import Sequence, Union
import uuid as _uuid
from datetime import datetime

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "v1_1_0"
down_revision: Union[str, Sequence[str], None] = "v1_0_0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


REVIEWER_PERMISSIONS = (
    "reports:read",
    "reports:approve",
    "reports:sign",
    "lab:read",
    "lab:manage_reviewers",
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. DDL: app_user.signature_storage_id
    # ------------------------------------------------------------------
    op.add_column(
        "app_user",
        sa.Column(
            "signature_storage_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "app_user_signature_storage_id_fkey",
        source_table="app_user",
        referent_table="storage_object",
        local_cols=["signature_storage_id"],
        remote_cols=["id"],
    )

    # ------------------------------------------------------------------
    # 2. DML: seed 'reviewer' role
    # ------------------------------------------------------------------
    role_table = sa.table(
        "role",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("created_at", sa.DateTime),
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("description", sa.String),
        sa.column("is_system", sa.Boolean),
        sa.column("is_protected", sa.Boolean),
        sa.column("tenant_id", postgresql.UUID(as_uuid=True)),
    )

    reviewer_role_id = _uuid.uuid4()
    op.bulk_insert(role_table, [{
        "id": reviewer_role_id,
        "created_at": datetime.utcnow(),
        "code": "reviewer",
        "name": "Revisor",
        "description": "Revisión y firma digital de informes patológicos.",
        "is_system": True,
        "is_protected": False,
        "tenant_id": None,
    }])

    # ------------------------------------------------------------------
    # 3. DML: seed reviewer's permissions
    # ------------------------------------------------------------------
    op.execute(
        f"""
        INSERT INTO public.role_permission (role_id, permission_id)
        SELECT r.id, p.id
        FROM public.role r
        CROSS JOIN public.permission p
        WHERE r.code = 'reviewer'
          AND p.code IN ({", ".join(f"'{c}'" for c in REVIEWER_PERMISSIONS)})
        """
    )

    # ------------------------------------------------------------------
    # 4. DML: revoke 'reports:sign' from 'pathologist'
    # ------------------------------------------------------------------
    op.execute(
        """
        DELETE FROM public.role_permission rp
        USING public.role r, public.permission p
        WHERE rp.role_id = r.id
          AND rp.permission_id = p.id
          AND r.code = 'pathologist'
          AND p.code = 'reports:sign'
        """
    )


def downgrade() -> None:
    # ------------------------------------------------------------------
    # 1. DML: restore 'reports:sign' on 'pathologist'
    # ------------------------------------------------------------------
    op.execute(
        """
        INSERT INTO public.role_permission (role_id, permission_id)
        SELECT r.id, p.id
        FROM public.role r
        CROSS JOIN public.permission p
        WHERE r.code = 'pathologist'
          AND p.code = 'reports:sign'
          AND NOT EXISTS (
              SELECT 1
              FROM public.role_permission existing
              WHERE existing.role_id = r.id
                AND existing.permission_id = p.id
          )
        """
    )

    # ------------------------------------------------------------------
    # 2. DML: drop reviewer role-permission rows + role
    # ------------------------------------------------------------------
    op.execute(
        """
        DELETE FROM public.role_permission
        WHERE role_id IN (SELECT id FROM public.role WHERE code = 'reviewer')
        """
    )
    op.execute("DELETE FROM public.role WHERE code = 'reviewer'")

    # ------------------------------------------------------------------
    # 3. DDL: drop app_user.signature_storage_id
    # ------------------------------------------------------------------
    op.drop_constraint(
        "app_user_signature_storage_id_fkey",
        "app_user",
        type_="foreignkey",
    )
    op.drop_column("app_user", "signature_storage_id")
