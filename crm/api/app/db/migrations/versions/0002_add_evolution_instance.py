"""add evolution_instance to corretoras

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-03

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "corretoras",
        sa.Column("evolution_instance", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("corretoras", "evolution_instance")
