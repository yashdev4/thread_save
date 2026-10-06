"""fidelity_reported_rank

B7 E-truth: insert `reported` (rank 4) below `verbatim`, which moves to rank 5.
Existing rows stored as verbatim (4) are shifted to 5 so their meaning is unchanged.

Revision ID: b7e1f0a2c3d4
Revises: 7a1b2c3d4e5f
Create Date: 2026-10-06 00:30:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b7e1f0a2c3d4'
down_revision: Union[str, Sequence[str], None] = '7a1b2c3d4e5f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
    UPDATE turns SET fidelity = 5 WHERE fidelity = 4;
    """)


def downgrade() -> None:
    # reported (4) has no older equivalent; it folds back into verbatim.
    op.execute("""
    UPDATE turns SET fidelity = 4 WHERE fidelity = 5;
    """)
