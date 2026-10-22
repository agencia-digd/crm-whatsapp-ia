"""add hora_tarefa (TIME NULL) em tarefas_corretora — horário opcional na tarefa

data_tarefa (DATE) segue como está. hora_tarefa é nullable: tarefas antigas
ficam sem hora (só data), sem quebrar nada.

Revision ID: 0047
Revises: 0046
Create Date: 2026-07-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0047"
down_revision: Union[str, None] = "0046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tarefas_corretora",
        sa.Column("hora_tarefa", sa.Time(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tarefas_corretora", "hora_tarefa")
