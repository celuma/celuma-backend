"""rename_lab_order_labels_to_order_labels

Revision ID: 0694f373f60a
Revises: 16fa16f3cf22
Create Date: 2026-01-22 22:14:58.734313

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0694f373f60a'
down_revision: Union[str, Sequence[str], None] = '16fa16f3cf22'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Rename lab_order_labels table to order_labels."""
    # Rename table
    op.rename_table('lab_order_labels', 'order_labels')


def downgrade() -> None:
    """Revert order_labels table back to lab_order_labels."""
    # Rename table back
    op.rename_table('order_labels', 'lab_order_labels')
