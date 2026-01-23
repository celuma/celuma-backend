"""restructure_schema_decouple_reviewers_rename_events

Revision ID: 7b744c59c3ad
Revises: 270866acafe7
Create Date: 2026-01-22 21:43:08.108409

Major schema restructuring:
1. Decouple reviewers from assignments - remove is_reviewer from assignment table
2. Add report_id to report_review table (nullable FK to report)
3. Rename case_event table to order_event
4. Add order_event compatible fields to audit_log
5. Normalize personal data fields in app_user and patient tables
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '7b744c59c3ad'
down_revision: Union[str, Sequence[str], None] = '270866acafe7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Upgrade schema:
    1. Remove is_reviewer column and index from assignment table
    2. Update unique index on assignment to remove is_reviewer
    3. Add report_id to report_review table
    4. Rename case_event table to order_event
    5. Add order_event compatible fields to audit_log
    6. Add first_name, last_name to app_user
    7. Add full_name, address to patient
    """
    
    # 1. Remove is_reviewer from assignment table
    op.drop_index('ix_assignment_is_reviewer', 'assignment')
    op.drop_index('ix_assignment_unique_active', 'assignment')
    
    # Delete assignments where is_reviewer=True (these will be managed in report_review table)
    op.execute("""
        DELETE FROM assignment WHERE is_reviewer = TRUE
    """)
    
    op.drop_column('assignment', 'is_reviewer')
    
    # Recreate unique index without is_reviewer
    op.execute("""
        CREATE UNIQUE INDEX ix_assignment_unique_active 
        ON assignment (tenant_id, item_type, item_id, assignee_user_id) 
        WHERE unassigned_at IS NULL
    """)
    
    # 2. Add report_id to report_review table
    op.add_column(
        'report_review',
        sa.Column('report_id', postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        'fk_report_review_report_id',
        'report_review', 'report',
        ['report_id'], ['id']
    )
    op.create_index('ix_report_review_report_id', 'report_review', ['report_id'])
    
    # Update unique index to use order_id instead of report_id
    op.drop_index('ix_report_review_unique_pending', 'report_review')
    op.execute("""
        CREATE UNIQUE INDEX ix_report_review_unique_pending 
        ON report_review (tenant_id, order_id, reviewer_user_id) 
        WHERE status = 'PENDING'
    """)
    
    # 3. Rename case_event table to order_event
    op.rename_table('case_event', 'order_event')
    
    # Recreate indexes with new table name
    op.execute("ALTER INDEX IF EXISTS ix_case_event_sample_id RENAME TO ix_order_event_sample_id")
    
    # 4. Add order_event compatible fields to audit_log
    op.add_column(
        'audit_log',
        sa.Column('order_id', postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        'audit_log',
        sa.Column('sample_id', postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        'audit_log',
        sa.Column('event_type', 
                  postgresql.ENUM('ORDER_CREATED', 'ORDER_DELIVERED', 'ORDER_CANCELLED', 
                                'ORDER_STATUS_CHANGED', 'ORDER_NOTES_UPDATED',
                                'SAMPLE_CREATED', 'SAMPLE_RECEIVED', 'SAMPLE_PREPARED',
                                'SAMPLE_STATE_CHANGED', 'SAMPLE_NOTES_UPDATED',
                                'SAMPLE_DAMAGED', 'SAMPLE_CANCELLED',
                                'IMAGE_UPLOADED', 'IMAGE_DELETED',
                                'REPORT_CREATED', 'REPORT_VERSION_CREATED', 'REPORT_SUBMITTED',
                                'REPORT_APPROVED', 'REPORT_CHANGES_REQUESTED', 'REPORT_SIGNED',
                                'REPORT_PUBLISHED', 'REPORT_RETRACTED',
                                'INVOICE_CREATED', 'PAYMENT_RECEIVED',
                                'ASSIGNEES_ADDED', 'ASSIGNEES_REMOVED', 'REVIEWERS_ADDED',
                                'REVIEWERS_REMOVED', 'LABELS_ADDED', 'LABELS_REMOVED',
                                'STATUS_CHANGED', 'NOTE_ADDED', 'COMMENT_ADDED',
                                name='eventtype', create_type=False),
                  nullable=True)
    )
    op.add_column(
        'audit_log',
        sa.Column('description', sa.String(length=500), nullable=True)
    )
    op.add_column(
        'audit_log',
        sa.Column('event_metadata', sa.JSON(), nullable=True)
    )
    op.add_column(
        'audit_log',
        sa.Column('created_by', postgresql.UUID(as_uuid=True), nullable=True)
    )
    
    # Create foreign keys for new audit_log fields
    op.create_foreign_key(
        'fk_audit_log_order_id',
        'audit_log', 'lab_order',
        ['order_id'], ['id']
    )
    op.create_foreign_key(
        'fk_audit_log_sample_id',
        'audit_log', 'sample',
        ['sample_id'], ['id']
    )
    op.create_foreign_key(
        'fk_audit_log_created_by',
        'audit_log', 'app_user',
        ['created_by'], ['id']
    )
    
    # Create indexes for audit_log new fields
    op.create_index('ix_audit_log_order_id', 'audit_log', ['order_id'])
    op.create_index('ix_audit_log_sample_id', 'audit_log', ['sample_id'])
    op.create_index('ix_audit_log_event_type', 'audit_log', ['event_type'])
    
    # 5. Add first_name, last_name to app_user
    op.add_column(
        'app_user',
        sa.Column('first_name', sa.String(length=255), nullable=True)
    )
    op.add_column(
        'app_user',
        sa.Column('last_name', sa.String(length=255), nullable=True)
    )
    
    # 6. Add full_name, address to patient
    op.add_column(
        'patient',
        sa.Column('full_name', sa.String(length=255), nullable=True)
    )
    op.add_column(
        'patient',
        sa.Column('address', sa.String(length=500), nullable=True)
    )


def downgrade() -> None:
    """
    Downgrade schema:
    Reverse all changes made in upgrade
    """
    
    # 6. Remove full_name, address from patient
    op.drop_column('patient', 'address')
    op.drop_column('patient', 'full_name')
    
    # 5. Remove first_name, last_name from app_user
    op.drop_column('app_user', 'last_name')
    op.drop_column('app_user', 'first_name')
    
    # 4. Remove order_event compatible fields from audit_log
    op.drop_index('ix_audit_log_event_type', 'audit_log')
    op.drop_index('ix_audit_log_sample_id', 'audit_log')
    op.drop_index('ix_audit_log_order_id', 'audit_log')
    
    op.drop_constraint('fk_audit_log_created_by', 'audit_log', type_='foreignkey')
    op.drop_constraint('fk_audit_log_sample_id', 'audit_log', type_='foreignkey')
    op.drop_constraint('fk_audit_log_order_id', 'audit_log', type_='foreignkey')
    
    op.drop_column('audit_log', 'created_by')
    op.drop_column('audit_log', 'event_metadata')
    op.drop_column('audit_log', 'description')
    op.drop_column('audit_log', 'event_type')
    op.drop_column('audit_log', 'sample_id')
    op.drop_column('audit_log', 'order_id')
    
    # 3. Rename order_event back to case_event
    op.execute("ALTER INDEX IF EXISTS ix_order_event_sample_id RENAME TO ix_case_event_sample_id")
    op.rename_table('order_event', 'case_event')
    
    # 2. Remove report_id from report_review
    op.drop_index('ix_report_review_unique_pending', 'report_review')
    op.drop_index('ix_report_review_report_id', 'report_review')
    op.drop_constraint('fk_report_review_report_id', 'report_review', type_='foreignkey')
    op.drop_column('report_review', 'report_id')
    
    # Recreate old unique index with report_id
    op.execute("""
        CREATE UNIQUE INDEX ix_report_review_unique_pending 
        ON report_review (tenant_id, report_id, reviewer_user_id) 
        WHERE status = 'PENDING'
    """)
    
    # 1. Add back is_reviewer to assignment
    op.drop_index('ix_assignment_unique_active', 'assignment')
    
    op.add_column(
        'assignment',
        sa.Column('is_reviewer', sa.Boolean(), nullable=False, server_default='false')
    )
    op.create_index('ix_assignment_is_reviewer', 'assignment', ['is_reviewer'])
    
    # Recreate unique index with is_reviewer
    op.execute("""
        CREATE UNIQUE INDEX ix_assignment_unique_active 
        ON assignment (tenant_id, item_type, item_id, assignee_user_id, is_reviewer) 
        WHERE unassigned_at IS NULL
    """)
