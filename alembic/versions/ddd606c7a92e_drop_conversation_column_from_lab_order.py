"""drop_conversation_column_from_lab_order

Revision ID: ddd606c7a92e
Revises: 3bf82f2c78b7
Create Date: 2026-01-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON


# revision identifiers, used by Alembic.
revision: str = 'ddd606c7a92e'
down_revision: Union[str, Sequence[str], None] = '3bf82f2c78b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop conversation JSON column from lab_order.
    
    BREAKING CHANGE: This removes the conversation field entirely.
    Data should be migrated to order_comment table before running this migration.
    """
    op.drop_column('lab_order', 'conversation')


def downgrade() -> None:
    """Re-add conversation JSON column to lab_order."""
    op.add_column('lab_order', sa.Column('conversation', JSON, nullable=True))
