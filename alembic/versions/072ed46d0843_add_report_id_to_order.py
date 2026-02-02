"""Add report_id to order table

Revision ID: 072ed46d0843
Revises: 0694f373f60a
Create Date: 2026-02-04 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '072ed46d0843'
down_revision: Union[str, Sequence[str], None] = '0694f373f60a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add report_id column to order table and backfill existing data."""
    # Add report_id column to order table (nullable, FK to report.id)
    op.add_column('order', sa.Column('report_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        'fk_order_report_id',
        'order',
        'report',
        ['report_id'],
        ['id']
    )
    
    # Add unique constraint to enforce 1-to-1 relationship
    op.create_unique_constraint('uq_order_report_id', 'order', ['report_id'])
    
    # Backfill: update existing orders with their report_id
    op.execute("""
        UPDATE "order" o
        SET report_id = r.id
        FROM report r
        WHERE r.order_id = o.id
    """)


def downgrade() -> None:
    """Remove report_id column from order table."""
    op.drop_constraint('uq_order_report_id', 'order', type_='unique')
    op.drop_constraint('fk_order_report_id', 'order', type_='foreignkey')
    op.drop_column('order', 'report_id')
