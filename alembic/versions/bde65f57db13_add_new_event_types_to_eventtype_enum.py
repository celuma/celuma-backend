"""add new event types to eventtype enum

Revision ID: bde65f57db13
Revises: 47585a3afea4
Create Date: 2026-01-15 23:08:50.566002

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bde65f57db13'
down_revision: Union[str, Sequence[str], None] = '47585a3afea4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add new event types to the eventtype enum.
    
    New values:
    - SAMPLE_CREATED, SAMPLE_STATE_CHANGED, SAMPLE_DAMAGED, SAMPLE_CANCELLED
    - IMAGE_DELETED
    - ORDER_STATUS_CHANGED
    - REPORT_PUBLISHED
    - COMMENT_ADDED
    """
    # PostgreSQL allows adding values to an existing enum
    # Each ADD VALUE must be in its own transaction when not using IF NOT EXISTS
    op.execute("ALTER TYPE eventtype ADD VALUE IF NOT EXISTS 'SAMPLE_CREATED'")
    op.execute("ALTER TYPE eventtype ADD VALUE IF NOT EXISTS 'SAMPLE_STATE_CHANGED'")
    op.execute("ALTER TYPE eventtype ADD VALUE IF NOT EXISTS 'SAMPLE_DAMAGED'")
    op.execute("ALTER TYPE eventtype ADD VALUE IF NOT EXISTS 'SAMPLE_CANCELLED'")
    op.execute("ALTER TYPE eventtype ADD VALUE IF NOT EXISTS 'IMAGE_DELETED'")
    op.execute("ALTER TYPE eventtype ADD VALUE IF NOT EXISTS 'ORDER_STATUS_CHANGED'")
    op.execute("ALTER TYPE eventtype ADD VALUE IF NOT EXISTS 'REPORT_PUBLISHED'")
    op.execute("ALTER TYPE eventtype ADD VALUE IF NOT EXISTS 'COMMENT_ADDED'")


def downgrade() -> None:
    """Cannot remove values from PostgreSQL enum types.
    
    PostgreSQL does not support removing values from an enum.
    The downgrade would require recreating the enum type and updating all references.
    For safety, we leave this as a no-op.
    """
    pass
