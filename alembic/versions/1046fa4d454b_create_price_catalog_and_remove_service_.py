"""create_price_catalog_and_remove_service_catalog

Revision ID: 1046fa4d454b
Revises: d2e3f4a5b6c7
Create Date: 2026-02-05 18:39:19.637867

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '1046fa4d454b'
down_revision: Union[str, Sequence[str], None] = 'd2e3f4a5b6c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Create price_catalog table
    op.create_table(
        'price_catalog',
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('study_type_id', sa.Uuid(), nullable=False),
        sa.Column('unit_price', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('currency', sqlmodel.sql.sqltypes.AutoString(length=3), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('effective_from', sa.DateTime(), nullable=True),
        sa.Column('effective_to', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
        sa.ForeignKeyConstraint(['study_type_id'], ['study_type.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_price_catalog_tenant_id'), 'price_catalog', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_price_catalog_study_type_id'), 'price_catalog', ['study_type_id'], unique=False)
    
    # Remove service_id FK and column from invoice_item
    op.drop_constraint('invoice_item_service_id_fkey', 'invoice_item', type_='foreignkey')
    op.drop_column('invoice_item', 'service_id')
    
    # Drop service_catalog table
    op.drop_table('service_catalog')


def downgrade() -> None:
    """Downgrade schema."""
    # Recreate service_catalog table
    op.create_table(
        'service_catalog',
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('service_name', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column('service_code', sqlmodel.sql.sqltypes.AutoString(length=50), nullable=False),
        sa.Column('description', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('price', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('currency', sqlmodel.sql.sqltypes.AutoString(length=3), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('valid_from', sa.DateTime(), nullable=True),
        sa.Column('valid_until', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenant.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Re-add service_id to invoice_item
    op.add_column('invoice_item', sa.Column('service_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('invoice_item_service_id_fkey', 'invoice_item', 'service_catalog', ['service_id'], ['id'])
    
    # Drop price_catalog table
    op.drop_index(op.f('ix_price_catalog_study_type_id'), table_name='price_catalog')
    op.drop_index(op.f('ix_price_catalog_tenant_id'), table_name='price_catalog')
    op.drop_table('price_catalog')
