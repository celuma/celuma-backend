"""add study_type_id to order and default_report_template_id to study_type

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-02-04 10:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd2e3f4a5b6c7'
down_revision: Union[str, Sequence[str], None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add study_type_id to order and default_report_template_id to study_type."""
    # Add study_type_id to order table
    op.add_column('order', sa.Column('study_type_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        'fk_order_study_type',
        'order', 
        'study_type',
        ['study_type_id'], 
        ['id']
    )
    op.create_index('idx_order_study_type', 'order', ['study_type_id'])
    
    # Add default_report_template_id to study_type table
    op.add_column('study_type', sa.Column('default_report_template_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        'fk_study_type_report_template',
        'study_type',
        'report_template',
        ['default_report_template_id'],
        ['id']
    )
    op.create_index('idx_study_type_template', 'study_type', ['default_report_template_id'])


def downgrade() -> None:
    """Remove study_type_id from order and default_report_template_id from study_type."""
    # Drop study_type columns
    op.drop_index('idx_study_type_template', table_name='study_type')
    op.drop_constraint('fk_study_type_report_template', 'study_type', type_='foreignkey')
    op.drop_column('study_type', 'default_report_template_id')
    
    # Drop order columns
    op.drop_index('idx_order_study_type', table_name='order')
    op.drop_constraint('fk_order_study_type', 'order', type_='foreignkey')
    op.drop_column('order', 'study_type_id')
