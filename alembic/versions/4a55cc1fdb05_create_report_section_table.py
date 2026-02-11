"""create_report_section_table

Revision ID: 4a55cc1fdb05
Revises: 8fc3345aac2d
Create Date: 2026-02-09 18:43:50.052931

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision: str = '4a55cc1fdb05'
down_revision: Union[str, Sequence[str], None] = '8fc3345aac2d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create report_section table."""
    op.create_table('report_section',
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=True),
        sa.Column('section', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column('description', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('predefined_text', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.ForeignKeyConstraint(['created_by'], ['app_user.id'], ),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Create index for efficient querying by tenant
    op.create_index('idx_report_section_tenant', 'report_section', ['tenant_id'])


def downgrade() -> None:
    """Drop report_section table."""
    op.drop_index('idx_report_section_tenant', table_name='report_section')
    op.drop_table('report_section')
