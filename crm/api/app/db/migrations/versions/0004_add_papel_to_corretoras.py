"""add papel to corretoras (Assistente migração — Onda 1)

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-09

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "corretoras",
        sa.Column(
            "papel",
            sa.Text(),
            nullable=False,
            server_default="corretora_solo",
        ),
    )
    # Pessoa3 trabalha em par com Pessoa Pessoa9 (Pessoa visita, Pessoa3 atende).
    op.execute(
        "UPDATE corretoras SET papel = 'corretora_atendedora_de_dona' "
        "WHERE nome = 'Pessoa3'"
    )


def downgrade() -> None:
    op.drop_column("corretoras", "papel")
