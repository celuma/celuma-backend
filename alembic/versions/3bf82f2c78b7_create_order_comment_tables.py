"""create_order_comment_tables

Revision ID: 3bf82f2c78b7
Revises: ed1278111e9a
Create Date: 2026-01-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB


# revision identifiers, used by Alembic.
revision: str = '3bf82f2c78b7'
down_revision: Union[str, Sequence[str], None] = 'ed1278111e9a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create order_comment and order_comment_mention tables."""
    
    # Create order_comment table
    op.create_table(
        'order_comment',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', UUID(as_uuid=True), nullable=False),
        sa.Column('branch_id', UUID(as_uuid=True), nullable=False),
        sa.Column('order_id', UUID(as_uuid=True), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('created_by', UUID(as_uuid=True), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('comment_metadata', JSONB, nullable=False, server_default='{}'),
        sa.Column('edited_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('deleted_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id']),
        sa.ForeignKeyConstraint(['branch_id'], ['branch.id']),
        sa.ForeignKeyConstraint(['order_id'], ['lab_order.id']),
        sa.ForeignKeyConstraint(['created_by'], ['app_user.id']),
    )
    
    # Create indexes for order_comment
    op.create_index(
        'idx_order_comment_order_cursor',
        'order_comment',
        ['tenant_id', 'order_id', sa.text('created_at DESC'), sa.text('id DESC')]
    )
    op.create_index(
        'idx_order_comment_created_by',
        'order_comment',
        ['tenant_id', 'created_by', sa.text('created_at DESC')]
    )
    
    # Create order_comment_mention table
    op.create_table(
        'order_comment_mention',
        sa.Column('comment_id', UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint('comment_id', 'user_id'),
        sa.ForeignKeyConstraint(['comment_id'], ['order_comment.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['app_user.id']),
    )
    
    # Create index for order_comment_mention
    op.create_index(
        'idx_order_comment_mention_user',
        'order_comment_mention',
        ['user_id']
    )


def downgrade() -> None:
    """Drop order_comment and order_comment_mention tables."""
    
    # Drop indexes first
    op.drop_index('idx_order_comment_mention_user', table_name='order_comment_mention')
    op.drop_index('idx_order_comment_created_by', table_name='order_comment')
    op.drop_index('idx_order_comment_order_cursor', table_name='order_comment')
    
    # Drop tables
    op.drop_table('order_comment_mention')
    op.drop_table('order_comment')
