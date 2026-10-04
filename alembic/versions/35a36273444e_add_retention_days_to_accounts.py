"""add_retention_days_to_accounts

Revision ID: 35a36273444e
Revises: 978b10d4cad9
Create Date: 2026-10-04 17:56:55.526682

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '35a36273444e'
down_revision: Union[str, Sequence[str], None] = '978b10d4cad9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE accounts ADD COLUMN IF NOT EXISTS retention_days integer DEFAULT NULL;
    """)


def downgrade() -> None:
    op.execute("""
    ALTER TABLE accounts DROP COLUMN IF EXISTS retention_days;
    """)
