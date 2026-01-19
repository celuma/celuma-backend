"""add report version and retracted event types

Revision ID: b3dc986e984c
Revises: bde65f57db13
Create Date: 2026-01-16 00:25:59.531760

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3dc986e984c'
down_revision: Union[str, Sequence[str], None] = 'bde65f57db13'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add new event types to eventtype enum
    op.execute("ALTER TYPE eventtype ADD VALUE IF NOT EXISTS 'REPORT_VERSION_CREATED'")
    op.execute("ALTER TYPE eventtype ADD VALUE IF NOT EXISTS 'REPORT_RETRACTED'")


def downgrade() -> None:
    """Downgrade schema."""
    # Downgrade for ENUMs in PostgreSQL is complex and usually involves recreating the type.
    # For simplicity, we leave this as a pass since removing enum values is rarely needed.
    pass
