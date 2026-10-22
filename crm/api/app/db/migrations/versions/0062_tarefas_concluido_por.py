"""tarefas_corretora.concluido_por — quem concluiu a tarefa (login do painel)

Alimenta a aba "Concluídas" da tela ☑️ Tarefas do V2 (histórico paginado com
quem/quando). Gravado pelo POST /painel/tarefa/{id}/concluir. Linhas antigas
ficam NULL (front mostra "—").

Revision ID: 0062
Revises: 0061
Create Date: 2026-07-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0062"
down_revision: Union[str, None] = "0061"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tarefas_corretora",
        sa.Column("concluido_por", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tarefas_corretora", "concluido_por")
