"""create study_type table

Revision ID: c1d2e3f4a5b6
Revises: 072ed46d0843
Create Date: 2026-02-04 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, Sequence[str], None] = '072ed46d0843'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create study_type table."""
    op.create_table('study_type',
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('code', sqlmodel.sql.sqltypes.AutoString(length=50), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column('description', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Create indexes for efficient querying
    op.create_index('idx_study_type_tenant', 'study_type', ['tenant_id'])
    op.create_index('idx_study_type_tenant_active', 'study_type', ['tenant_id', 'is_active'])
    op.create_index('idx_study_type_code', 'study_type', ['code'])


def downgrade() -> None:
    """Drop study_type table."""
    op.drop_index('idx_study_type_code', table_name='study_type')
    op.drop_index('idx_study_type_tenant_active', table_name='study_type')
    op.drop_index('idx_study_type_tenant', table_name='study_type')
    op.drop_table('study_type')
