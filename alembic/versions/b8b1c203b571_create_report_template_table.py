"""create_report_template_table

Revision ID: b8b1c203b571
Revises: 6c4ffcee433a
Create Date: 2026-01-21 19:24:05.454384

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision: str = 'b8b1c203b571'
down_revision: Union[str, Sequence[str], None] = '6c4ffcee433a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create report_template table."""
    op.create_table('report_template',
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column('description', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('template_json', sa.JSON(), nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['created_by'], ['app_user.id'], ),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Create indexes for efficient querying
    op.create_index('idx_report_template_tenant', 'report_template', ['tenant_id'])
    op.create_index('idx_report_template_tenant_active', 'report_template', ['tenant_id', 'is_active'])


def downgrade() -> None:
    """Drop report_template table."""
    op.drop_index('idx_report_template_tenant_active', table_name='report_template')
    op.drop_index('idx_report_template_tenant', table_name='report_template')
    op.drop_table('report_template')
