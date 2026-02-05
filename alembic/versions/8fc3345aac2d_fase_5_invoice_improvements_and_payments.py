"""fase_5_invoice_improvements_and_payments

Revision ID: 8fc3345aac2d
Revises: 1046fa4d454b
Create Date: 2026-02-05 19:56:45.164600

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '8fc3345aac2d'
down_revision: Union[str, Sequence[str], None] = '1046fa4d454b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Add VOID to PaymentStatus enum
    op.execute("ALTER TYPE paymentstatus ADD VALUE IF NOT EXISTS 'VOID'")
    
    # 2. Add new columns to invoice table
    op.add_column('invoice', sa.Column('subtotal', sa.Numeric(precision=12, scale=2), nullable=True))
    op.add_column('invoice', sa.Column('discount_total', sa.Numeric(precision=12, scale=2), nullable=True))
    op.add_column('invoice', sa.Column('tax_total', sa.Numeric(precision=12, scale=2), nullable=True))
    op.add_column('invoice', sa.Column('total', sa.Numeric(precision=12, scale=2), nullable=True))
    op.add_column('invoice', sa.Column('amount_paid', sa.Numeric(precision=12, scale=2), nullable=True))
    op.add_column('invoice', sa.Column('updated_at', sa.DateTime(), nullable=True))
    op.add_column('invoice', sa.Column('paid_at', sa.DateTime(), nullable=True))
    
    # Set defaults for existing invoices
    op.execute("""
        UPDATE invoice SET
            subtotal = amount_total,
            discount_total = 0,
            tax_total = 0,
            total = amount_total,
            amount_paid = 0,
            updated_at = created_at
        WHERE subtotal IS NULL
    """)
    
    # Make columns non-nullable
    op.alter_column('invoice', 'subtotal', nullable=False)
    op.alter_column('invoice', 'discount_total', nullable=False)
    op.alter_column('invoice', 'tax_total', nullable=False)
    op.alter_column('invoice', 'total', nullable=False)
    op.alter_column('invoice', 'amount_paid', nullable=False)
    
    # Create unique constraint on order_id
    op.create_unique_constraint('uq_invoice_order_id', 'invoice', ['order_id'])
    
    # 3. Add study_type_id to invoice_item table
    op.add_column('invoice_item', sa.Column('study_type_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('invoice_item_study_type_id_fkey', 'invoice_item', 'study_type', ['study_type_id'], ['id'])
    
    # 4. Modify payment table
    # Add new columns
    op.add_column('payment', sa.Column('currency', sqlmodel.sql.sqltypes.AutoString(length=3), nullable=True))
    op.add_column('payment', sa.Column('reference', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True))
    op.add_column('payment', sa.Column('created_by', sa.Uuid(), nullable=True))
    op.add_column('payment', sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=True))
    op.add_column('payment', sa.Column('received_at', sa.DateTime(), nullable=True))
    
    # Migrate data from old columns to new
    op.execute("UPDATE payment SET currency = 'MXN' WHERE currency IS NULL")
    op.execute("UPDATE payment SET amount = amount_paid WHERE amount IS NULL")
    op.execute("UPDATE payment SET received_at = paid_at WHERE received_at IS NULL")
    
    # Make new columns non-nullable
    op.alter_column('payment', 'currency', nullable=False)
    op.alter_column('payment', 'amount', nullable=False)
    op.alter_column('payment', 'received_at', nullable=False)
    
    # Add FK for created_by
    op.create_foreign_key('payment_created_by_fkey', 'payment', 'app_user', ['created_by'], ['id'])
    
    # Drop old columns
    op.drop_column('payment', 'amount_paid')
    op.drop_column('payment', 'paid_at')
    op.drop_constraint('payment_branch_id_fkey', 'payment', type_='foreignkey')
    op.drop_column('payment', 'branch_id')
    
    # 5. Add invoice_id to order table
    op.add_column('order', sa.Column('invoice_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('order_invoice_id_fkey', 'order', 'invoice', ['invoice_id'], ['id'])


def downgrade() -> None:
    """Downgrade schema."""
    # Remove invoice_id from order
    op.drop_constraint('order_invoice_id_fkey', 'order', type_='foreignkey')
    op.drop_column('order', 'invoice_id')
    
    # Restore payment table
    op.add_column('payment', sa.Column('branch_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('payment_branch_id_fkey', 'payment', 'branch', ['branch_id'], ['id'])
    op.add_column('payment', sa.Column('paid_at', sa.DateTime(), nullable=True))
    op.add_column('payment', sa.Column('amount_paid', sa.Numeric(precision=12, scale=2), nullable=True))
    
    # Migrate data back
    op.execute("UPDATE payment SET amount_paid = amount WHERE amount_paid IS NULL")
    op.execute("UPDATE payment SET paid_at = received_at WHERE paid_at IS NULL")
    
    op.alter_column('payment', 'amount_paid', nullable=False)
    op.alter_column('payment', 'paid_at', nullable=False)
    
    # Drop new payment columns
    op.drop_constraint('payment_created_by_fkey', 'payment', type_='foreignkey')
    op.drop_column('payment', 'received_at')
    op.drop_column('payment', 'amount')
    op.drop_column('payment', 'created_by')
    op.drop_column('payment', 'reference')
    op.drop_column('payment', 'currency')
    
    # Remove study_type_id from invoice_item
    op.drop_constraint('invoice_item_study_type_id_fkey', 'invoice_item', type_='foreignkey')
    op.drop_column('invoice_item', 'study_type_id')
    
    # Remove unique constraint and new columns from invoice
    op.drop_constraint('uq_invoice_order_id', 'invoice', type_='unique')
    op.drop_column('invoice', 'paid_at')
    op.drop_column('invoice', 'updated_at')
    op.drop_column('invoice', 'amount_paid')
    op.drop_column('invoice', 'total')
    op.drop_column('invoice', 'tax_total')
    op.drop_column('invoice', 'discount_total')
    op.drop_column('invoice', 'subtotal')
    
    # Note: Cannot remove VOID from enum without recreating it
