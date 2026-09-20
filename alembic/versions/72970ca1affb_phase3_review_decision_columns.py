"""phase3 review decision columns

Revision ID: 72970ca1affb
Revises: 1e8f5722d2b9
Create Date: 2026-09-20 17:55:31.724369

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '72970ca1affb'
down_revision: Union[str, None] = '1e8f5722d2b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("review", sa.Column("original_content", sa.Text(), nullable=True))
    op.add_column("review", sa.Column("edited_content", sa.Text(), nullable=True))
    op.add_column("review", sa.Column("reject_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("review", "reject_reason")
    op.drop_column("review", "edited_content")
    op.drop_column("review", "original_content")
