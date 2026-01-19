"""add_sample_notes_updated_event

Revision ID: 570774bd4709
Revises: b3dc986e984c
Create Date: 2026-01-16 10:51:24.814900

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '570774bd4709'
down_revision: Union[str, Sequence[str], None] = 'b3dc986e984c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add SAMPLE_NOTES_UPDATED to EventType enum
    # Note: PostgreSQL requires a specific syntax for adding enum values
    op.execute("ALTER TYPE eventtype ADD VALUE IF NOT EXISTS 'SAMPLE_NOTES_UPDATED' AFTER 'SAMPLE_STATE_CHANGED'")


def downgrade() -> None:
    """Downgrade schema."""
    # Note: PostgreSQL does not support removing enum values directly
    # This would require recreating the enum type, which is complex
    # For now, we'll leave the value in the enum on downgrade
    pass
