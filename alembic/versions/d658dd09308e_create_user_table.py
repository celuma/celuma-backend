"""create user table

Revision ID: d658dd09308e
Revises: 283ec0b3dc04
Create Date: 2025-08-15 20:19:35.837327

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd658dd09308e'
down_revision: Union[str, Sequence[str], None] = '283ec0b3dc04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
