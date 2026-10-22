"""adiciona is_revisita em visitas

Revision ID: 0026
Revises: 0025
Create Date: 2026-05-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "visitas",
        sa.Column("is_revisita", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("idx_visitas_revisita", "visitas", ["is_revisita"])


def downgrade() -> None:
    op.drop_index("idx_visitas_revisita", table_name="visitas")
    op.drop_column("visitas", "is_revisita")
