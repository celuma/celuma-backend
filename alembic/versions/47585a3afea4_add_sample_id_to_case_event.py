"""add sample_id to case_event

Revision ID: 47585a3afea4
Revises: 84ec48962366
Create Date: 2026-01-15 23:03:08.701197

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '47585a3afea4'
down_revision: Union[str, Sequence[str], None] = '84ec48962366'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add sample_id column to case_event for sample-specific timeline filtering."""
    op.add_column('case_event', sa.Column('sample_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        'fk_case_event_sample_id',
        'case_event', 'sample',
        ['sample_id'], ['id']
    )
    # Create index for efficient filtering by sample_id
    op.create_index('ix_case_event_sample_id', 'case_event', ['sample_id'])


def downgrade() -> None:
    """Remove sample_id column from case_event."""
    op.drop_index('ix_case_event_sample_id', table_name='case_event')
    op.drop_constraint('fk_case_event_sample_id', 'case_event', type_='foreignkey')
    op.drop_column('case_event', 'sample_id')
