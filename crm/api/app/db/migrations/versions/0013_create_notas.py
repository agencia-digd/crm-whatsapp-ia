"""create notas — Onda P1

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "notas",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("contexto", sa.Text(), nullable=False),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("registrado_por_telefone", sa.Text(), nullable=True),
        sa.Column("registrado_em", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_notas_contexto_data", "notas",
                    ["contexto", sa.text("registrado_em DESC")])


def downgrade() -> None:
    op.drop_index("idx_notas_contexto_data", table_name="notas")
    op.drop_table("notas")
