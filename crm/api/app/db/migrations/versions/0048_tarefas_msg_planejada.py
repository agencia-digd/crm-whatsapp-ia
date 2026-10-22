"""add anotacoes + mensagem_planejada + msg_planejada_enviada_em em tarefas_corretora

Tarefa deixa de ser só lembrete: ganha ANOTAÇÕES (campo próprio, não reusa as
notas do lead) e MENSAGEM PLANEJADA (corpo que dispara sozinho na data/hora, em
UTILITY, pela linha da Pessoa12). Colunas todas NULL — tarefas antigas ficam intactas
(só lembrete). `msg_planejada_enviada_em` = idempotência do scheduler (NULL =
ainda não disparou).

Não mexe no que já existe (0047 add hora_tarefa).

Revision ID: 0048
Revises: 0047
Create Date: 2026-07-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0048"
down_revision: Union[str, None] = "0047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tarefas_corretora",
        sa.Column("anotacoes", sa.Text(), nullable=True),
    )
    op.add_column(
        "tarefas_corretora",
        sa.Column("mensagem_planejada", sa.Text(), nullable=True),
    )
    op.add_column(
        "tarefas_corretora",
        sa.Column(
            "msg_planejada_enviada_em",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("tarefas_corretora", "msg_planejada_enviada_em")
    op.drop_column("tarefas_corretora", "mensagem_planejada")
    op.drop_column("tarefas_corretora", "anotacoes")
