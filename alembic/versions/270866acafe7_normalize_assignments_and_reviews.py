"""normalize_assignments_and_reviews

Revision ID: 270866acafe7
Revises: b8b1c203b571
Create Date: 2026-01-22 17:59:48.064027

Creates normalized tables for assignments and report reviews:
- assignment: tracks user assignments to orders, samples, and reports
- report_review: tracks individual review decisions on reports

Removes embedded array columns:
- lab_order.assignees
- lab_order.reviewers
- sample.assignees

NO DATA MIGRATION - only schema changes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '270866acafe7'
down_revision: Union[str, Sequence[str], None] = 'b8b1c203b571'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Upgrade schema:
    1. Create assignment table with indexes
    2. Create review_status enum
    3. Create report_review table with indexes
    4. Drop old embedded columns from lab_order and sample
    """
    
    # 1. Create assignment_item_type enum (if not exists)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE assignmentitemtype AS ENUM ('lab_order', 'sample', 'report');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)
    
    # 2. Create assignment table
    op.create_table(
        'assignment',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('tenant.id'), nullable=False),
        sa.Column('item_type', postgresql.ENUM('lab_order', 'sample', 'report', name='assignmentitemtype', create_type=False), nullable=False),
        sa.Column('item_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('assignee_user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('app_user.id'), nullable=False),
        sa.Column('assigned_by_user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('app_user.id'), nullable=True),
        sa.Column('assigned_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('unassigned_at', sa.DateTime(), nullable=True),
        sa.Column('is_reviewer', sa.Boolean(), nullable=False, server_default='false'),
    )
    
    # Create indexes for assignment table
    op.create_index(
        'ix_assignment_tenant_id',
        'assignment',
        ['tenant_id']
    )
    op.create_index(
        'ix_assignment_tenant_assignee',
        'assignment',
        ['tenant_id', 'assignee_user_id', 'assigned_at'],
        postgresql_ops={'assigned_at': 'DESC'}
    )
    op.create_index(
        'ix_assignment_tenant_item',
        'assignment',
        ['tenant_id', 'item_type', 'item_id']
    )
    op.create_index(
        'ix_assignment_item',
        'assignment',
        ['item_type', 'item_id']
    )
    op.create_index(
        'ix_assignment_assignee',
        'assignment',
        ['assignee_user_id']
    )
    op.create_index(
        'ix_assignment_is_reviewer',
        'assignment',
        ['is_reviewer']
    )
    
    # Create partial unique index for active assignments
    op.execute("""
        CREATE UNIQUE INDEX ix_assignment_unique_active 
        ON assignment (tenant_id, item_type, item_id, assignee_user_id, is_reviewer) 
        WHERE unassigned_at IS NULL
    """)
    
    # 3. Create review_status enum (if not exists)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE reviewstatus AS ENUM ('PENDING', 'APPROVED', 'REJECTED');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)
    
    # 4. Create report_review table
    op.create_table(
        'report_review',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('tenant.id'), nullable=False),
        sa.Column('order_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('lab_order.id'), nullable=False),
        sa.Column('reviewer_user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('app_user.id'), nullable=False),
        sa.Column('assigned_by_user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('app_user.id'), nullable=True),
        sa.Column('assigned_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('decision_at', sa.DateTime(), nullable=True),
        sa.Column('status', postgresql.ENUM('PENDING', 'APPROVED', 'REJECTED', name='reviewstatus', create_type=False), nullable=False, server_default='PENDING'),
    )
    
    # Create indexes for report_review table
    op.create_index(
        'ix_report_review_tenant_id',
        'report_review',
        ['tenant_id']
    )
    op.create_index(
        'ix_report_review_tenant_reviewer',
        'report_review',
        ['tenant_id', 'reviewer_user_id', 'status', 'assigned_at'],
        postgresql_ops={'assigned_at': 'DESC'}
    )
    op.create_index(
        'ix_report_review_tenant_order',
        'report_review',
        ['tenant_id', 'order_id']
    )
    op.create_index(
        'ix_report_review_order',
        'report_review',
        ['order_id']
    )
    op.create_index(
        'ix_report_review_reviewer',
        'report_review',
        ['reviewer_user_id']
    )
    op.create_index(
        'ix_report_review_status',
        'report_review',
        ['status']
    )
    
    # Create partial unique index for pending reviews
    op.execute("""
        CREATE UNIQUE INDEX ix_report_review_unique_pending 
        ON report_review (tenant_id, order_id, reviewer_user_id) 
        WHERE status = 'PENDING'
    """)
    
    # 5. Drop old embedded columns from lab_order
    op.drop_column('lab_order', 'assignees')
    op.drop_column('lab_order', 'reviewers')
    
    # 6. Drop old embedded column from sample
    op.drop_column('sample', 'assignees')


def downgrade() -> None:
    """
    Downgrade schema:
    1. Re-create old embedded columns (empty/nullable)
    2. Drop report_review table and indexes
    3. Drop assignment table and indexes
    4. Drop enums
    
    NOTE: Does NOT restore data - columns will be empty after downgrade.
    """
    
    # 1. Re-create old embedded columns on lab_order (nullable, empty)
    op.add_column(
        'lab_order',
        sa.Column('assignees', postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=True)
    )
    op.add_column(
        'lab_order',
        sa.Column('reviewers', postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=True)
    )
    
    # 2. Re-create old embedded column on sample (nullable, empty)
    op.add_column(
        'sample',
        sa.Column('assignees', postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=True)
    )
    
    # 3. Drop report_review table and indexes
    op.drop_index('ix_report_review_unique_pending', 'report_review')
    op.drop_index('ix_report_review_status', 'report_review')
    op.drop_index('ix_report_review_reviewer', 'report_review')
    op.drop_index('ix_report_review_order', 'report_review')
    op.drop_index('ix_report_review_tenant_order', 'report_review')
    op.drop_index('ix_report_review_tenant_reviewer', 'report_review')
    op.drop_index('ix_report_review_tenant_id', 'report_review')
    op.drop_table('report_review')
    
    # 4. Drop assignment table and indexes
    op.drop_index('ix_assignment_unique_active', 'assignment')
    op.drop_index('ix_assignment_is_reviewer', 'assignment')
    op.drop_index('ix_assignment_assignee', 'assignment')
    op.drop_index('ix_assignment_item', 'assignment')
    op.drop_index('ix_assignment_tenant_item', 'assignment')
    op.drop_index('ix_assignment_tenant_assignee', 'assignment')
    op.drop_index('ix_assignment_tenant_id', 'assignment')
    op.drop_table('assignment')
    
    # 5. Drop enums
    op.execute('DROP TYPE IF EXISTS reviewstatus')
    op.execute('DROP TYPE IF EXISTS assignmentitemtype')
