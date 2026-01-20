"""add_assignees_reviewers_labels_feature

Revision ID: 6c4ffcee433a
Revises: ddd606c7a92e
Create Date: 2026-01-18 11:34:08.381191

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6c4ffcee433a'
down_revision: Union[str, Sequence[str], None] = 'ddd606c7a92e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - Add assignees, reviewers, and labels feature."""
    from sqlalchemy.dialects import postgresql
    
    # 1. Add new event types to EventType enum
    op.execute("ALTER TYPE eventtype ADD VALUE IF NOT EXISTS 'ASSIGNEES_ADDED'")
    op.execute("ALTER TYPE eventtype ADD VALUE IF NOT EXISTS 'ASSIGNEES_REMOVED'")
    op.execute("ALTER TYPE eventtype ADD VALUE IF NOT EXISTS 'REVIEWERS_ADDED'")
    op.execute("ALTER TYPE eventtype ADD VALUE IF NOT EXISTS 'REVIEWERS_REMOVED'")
    op.execute("ALTER TYPE eventtype ADD VALUE IF NOT EXISTS 'LABELS_ADDED'")
    op.execute("ALTER TYPE eventtype ADD VALUE IF NOT EXISTS 'LABELS_REMOVED'")
    
    # 2. Create label table
    op.create_table(
        'label',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('color', sa.String(length=7), nullable=False),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('tenant.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False)
    )
    
    # Add indexes for label table
    op.create_index('ix_label_tenant_id', 'label', ['tenant_id'])
    op.create_index('ix_label_name', 'label', ['name'])
    
    # 3. Create lab_order_labels junction table
    op.create_table(
        'lab_order_labels',
        sa.Column('order_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('lab_order.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('label_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('label.id', ondelete='CASCADE'), primary_key=True)
    )
    
    # Add indexes for lab_order_labels
    op.create_index('ix_lab_order_labels_order_id', 'lab_order_labels', ['order_id'])
    op.create_index('ix_lab_order_labels_label_id', 'lab_order_labels', ['label_id'])
    
    # 4. Create sample_labels junction table
    op.create_table(
        'sample_labels',
        sa.Column('sample_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('sample.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('label_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('label.id', ondelete='CASCADE'), primary_key=True)
    )
    
    # Add indexes for sample_labels
    op.create_index('ix_sample_labels_sample_id', 'sample_labels', ['sample_id'])
    op.create_index('ix_sample_labels_label_id', 'sample_labels', ['label_id'])
    
    # 5. Add assignees and reviewers columns to lab_order
    op.add_column('lab_order', sa.Column('assignees', postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=True))
    op.add_column('lab_order', sa.Column('reviewers', postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=True))
    
    # 6. Add assignees column to sample
    op.add_column('sample', sa.Column('assignees', postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    # Remove columns from sample
    op.drop_column('sample', 'assignees')
    
    # Remove columns from lab_order
    op.drop_column('lab_order', 'reviewers')
    op.drop_column('lab_order', 'assignees')
    
    # Drop indexes and tables
    op.drop_index('ix_sample_labels_label_id', 'sample_labels')
    op.drop_index('ix_sample_labels_sample_id', 'sample_labels')
    op.drop_table('sample_labels')
    
    op.drop_index('ix_lab_order_labels_label_id', 'lab_order_labels')
    op.drop_index('ix_lab_order_labels_order_id', 'lab_order_labels')
    op.drop_table('lab_order_labels')
    
    op.drop_index('ix_label_name', 'label')
    op.drop_index('ix_label_tenant_id', 'label')
    op.drop_table('label')
    
    # Note: Cannot remove enum values in PostgreSQL without recreating the type
    # Leaving event types as-is for safety
