"""add_conversation_field_to_lab_order

Revision ID: ed1278111e9a
Revises: 81f7b969b9a4
Create Date: 2026-01-16 11:40:32.691298

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON


# revision identifiers, used by Alembic.
revision: str = 'ed1278111e9a'
down_revision: Union[str, Sequence[str], None] = '81f7b969b9a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add conversation JSON field to lab_order table
    op.add_column('lab_order', sa.Column('conversation', JSON, nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    # Remove conversation field
    op.drop_column('lab_order', 'conversation')
