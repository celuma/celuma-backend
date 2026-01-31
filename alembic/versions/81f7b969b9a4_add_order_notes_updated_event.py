"""add_order_notes_updated_event

Revision ID: 81f7b969b9a4
Revises: 570774bd4709
Create Date: 2026-01-16 11:13:12.910230

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '81f7b969b9a4'
down_revision: Union[str, Sequence[str], None] = '570774bd4709'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add ORDER_NOTES_UPDATED to the eventtype enum
    op.execute("ALTER TYPE eventtype ADD VALUE 'ORDER_NOTES_UPDATED'")


def downgrade() -> None:
    """Downgrade schema."""
    # Removing an enum value is more complex and usually involves:
    # 1. Dropping dependent objects (tables/columns)
    # 2. Recreating the enum without the value
    # 3. Recreating dependent objects
    # For simplicity in this example, we'll recreate the enum
    op.execute("ALTER TYPE eventtype RENAME TO eventtype_old")
    op.execute("CREATE TYPE eventtype AS ENUM('ORDER_CREATED', 'ORDER_DELIVERED', 'ORDER_CANCELLED', 'ORDER_STATUS_CHANGED', 'SAMPLE_CREATED', 'SAMPLE_RECEIVED', 'SAMPLE_PREPARED', 'SAMPLE_STATE_CHANGED', 'SAMPLE_NOTES_UPDATED', 'SAMPLE_DAMAGED', 'SAMPLE_CANCELLED', 'IMAGE_UPLOADED', 'IMAGE_DELETED', 'REPORT_CREATED', 'REPORT_VERSION_CREATED', 'REPORT_SUBMITTED', 'REPORT_APPROVED', 'REPORT_CHANGES_REQUESTED', 'REPORT_SIGNED', 'REPORT_PUBLISHED', 'REPORT_RETRACTED', 'INVOICE_CREATED', 'PAYMENT_RECEIVED', 'STATUS_CHANGED', 'NOTE_ADDED', 'COMMENT_ADDED')")
    op.execute("ALTER TABLE order_event ALTER COLUMN event_type TYPE eventtype USING event_type::text::eventtype")
    op.execute("DROP TYPE eventtype_old")
