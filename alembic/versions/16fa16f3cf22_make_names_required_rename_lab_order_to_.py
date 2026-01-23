"""make_names_required_rename_lab_order_to_order

Revision ID: 16fa16f3cf22
Revises: 7b744c59c3ad
Create Date: 2026-01-22 21:55:42.043223

Changes:
1. Make first_name and last_name NOT NULL in app_user and patient tables
2. Rename lab_order table to order
3. Update all foreign key references
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '16fa16f3cf22'
down_revision: Union[str, Sequence[str], None] = '7b744c59c3ad'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Upgrade schema:
    1. Make first_name and last_name NOT NULL in app_user
    2. Make first_name and last_name NOT NULL in patient  
    3. Rename lab_order table to order
    """
    
    # 1. Make first_name and last_name NOT NULL in app_user
    # Set empty string for NULL values first
    op.execute("UPDATE app_user SET first_name = '' WHERE first_name IS NULL")
    op.execute("UPDATE app_user SET last_name = '' WHERE last_name IS NULL")
    
    op.alter_column('app_user', 'first_name',
                    existing_type=sa.String(length=255),
                    nullable=False,
                    server_default='')
    op.alter_column('app_user', 'last_name',
                    existing_type=sa.String(length=255),
                    nullable=False,
                    server_default='')
    
    # 2. Make first_name and last_name NOT NULL in patient (already NOT NULL, but full_name is nullable)
    # Patient first_name and last_name are already required, but full_name is nullable
    # Set full_name to first_name + last_name for existing records
    op.execute("UPDATE patient SET full_name = first_name || ' ' || last_name WHERE full_name IS NULL")
    
    # 3. Rename lab_order table to order
    op.rename_table('lab_order', 'order')


def downgrade() -> None:
    """
    Downgrade schema:
    1. Rename order back to lab_order
    2. Make first_name and last_name nullable in patient
    3. Make first_name and last_name nullable in app_user
    """
    
    # 1. Rename order back to lab_order
    op.rename_table('order', 'lab_order')
    
    # 2. Make first_name and last_name nullable in app_user
    op.alter_column('app_user', 'last_name',
                    existing_type=sa.String(length=255),
                    nullable=True)
    op.alter_column('app_user', 'first_name',
                    existing_type=sa.String(length=255),
                    nullable=True)
    
    # Patient fields remain unchanged as they were already NOT NULL
