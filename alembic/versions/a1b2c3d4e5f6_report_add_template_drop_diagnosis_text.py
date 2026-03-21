"""report: add template snapshot column, drop diagnosis_text

Revision ID: a1b2c3d4e5f6
Revises: 4a55cc1fdb05
Create Date: 2026-03-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '4a55cc1fdb05'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add template JSON snapshot column and remove deprecated diagnosis_text."""
    op.add_column('report', sa.Column('template', sa.JSON(), nullable=True))
    op.drop_column('report', 'diagnosis_text')


def downgrade() -> None:
    """Restore diagnosis_text and remove template column."""
    op.add_column('report', sa.Column('diagnosis_text', sa.Text(), nullable=True))
    op.drop_column('report', 'template')
