"""add sample state enum

Revision ID: 84ec48962366
Revises: 6965b748777e
Create Date: 2026-01-15 22:42:11.112661

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '84ec48962366'
down_revision: Union[str, Sequence[str], None] = '6965b748777e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Define enum types (consistent with other enums in the project)
samplestate_enum = postgresql.ENUM(
    'RECEIVED', 'PROCESSING', 'READY', 'DAMAGED', 'CANCELLED',
    name='samplestate',
    create_type=False
)

orderstatus_enum = postgresql.ENUM(
    'RECEIVED', 'PROCESSING', 'DIAGNOSIS', 'REVIEW', 'RELEASED', 'CLOSED', 'CANCELLED',
    name='orderstatus',
    create_type=False
)


def upgrade() -> None:
    """Migrate sample.state from OrderStatus enum to new SampleState enum.
    
    SampleState values: RECEIVED, PROCESSING, READY, DAMAGED, CANCELLED
    
    Mapping from OrderStatus:
    - RECEIVED -> RECEIVED
    - PROCESSING -> PROCESSING
    - CANCELLED -> CANCELLED
    - DIAGNOSIS, REVIEW, RELEASED, CLOSED -> READY
    """
    # Create the new enum type (consistent with how other enums are created)
    samplestate_enum.create(op.get_bind(), checkfirst=True)
    
    # Alter the column with value conversion using CASE
    op.execute("""
        ALTER TABLE sample 
        ALTER COLUMN state TYPE samplestate 
        USING (
            CASE state::text
                WHEN 'RECEIVED' THEN 'RECEIVED'::samplestate
                WHEN 'PROCESSING' THEN 'PROCESSING'::samplestate
                WHEN 'CANCELLED' THEN 'CANCELLED'::samplestate
                WHEN 'DIAGNOSIS' THEN 'READY'::samplestate
                WHEN 'REVIEW' THEN 'READY'::samplestate
                WHEN 'RELEASED' THEN 'READY'::samplestate
                WHEN 'CLOSED' THEN 'READY'::samplestate
                ELSE 'RECEIVED'::samplestate
            END
        )
    """)


def downgrade() -> None:
    """Revert sample.state back to OrderStatus enum."""
    # Alter column back to orderstatus with value conversion
    op.execute("""
        ALTER TABLE sample 
        ALTER COLUMN state TYPE orderstatus 
        USING (
            CASE state::text
                WHEN 'RECEIVED' THEN 'RECEIVED'::orderstatus
                WHEN 'PROCESSING' THEN 'PROCESSING'::orderstatus
                WHEN 'CANCELLED' THEN 'CANCELLED'::orderstatus
                WHEN 'READY' THEN 'CLOSED'::orderstatus
                WHEN 'DAMAGED' THEN 'CANCELLED'::orderstatus
                ELSE 'RECEIVED'::orderstatus
            END
        )
    """)
    
    # Drop the samplestate enum type
    samplestate_enum.drop(op.get_bind(), checkfirst=True)
