"""add_anchor_to_turns

Revision ID: 978b10d4cad9
Revises: ddd36e757df0
Create Date: 2026-10-04 17:22:11

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = '978b10d4cad9'
down_revision: Union[str, Sequence[str], None] = 'ddd36e757df0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE turns ADD COLUMN anchor text;")
    op.execute("CREATE INDEX turns_anchor_idx ON turns (anchor) WHERE role = 'user' AND anchor IS NOT NULL;")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS turns_anchor_idx;")
    op.execute("ALTER TABLE turns DROP COLUMN IF EXISTS anchor;")
