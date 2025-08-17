"""Add blacklisted tokens table

Revision ID: add_blacklisted_tokens
Revises: f45e096206ac
Create Date: 2025-01-17 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'add_blacklisted_tokens'
down_revision = 'f45e096206ac'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create blacklisted_token table
    op.create_table('blacklisted_token',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('token', sa.String(length=1000), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('blacklisted_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token')
    )
    
    # Create index on token for faster lookups
    op.create_index(op.f('ix_blacklisted_token_token'), 'blacklisted_token', ['token'], unique=True)
    
    # Create index on user_id for user-specific queries
    op.create_index(op.f('ix_blacklisted_token_user_id'), 'blacklisted_token', ['user_id'], unique=False)
    
    # Add foreign key constraint
    op.create_foreign_key(None, 'blacklisted_token', 'app_user', ['user_id'], ['id'])


def downgrade() -> None:
    # Remove foreign key constraint
    op.drop_constraint(None, 'blacklisted_token', type_='foreignkey')
    
    # Remove indexes
    op.drop_index(op.f('ix_blacklisted_token_user_id'), table_name='blacklisted_token')
    op.drop_index(op.f('ix_blacklisted_token_token'), table_name='blacklisted_token')
    
    # Remove table
    op.drop_table('blacklisted_token')
