"""pessoa_retomadas — turno da Pessoa sem LLM vira fila, não silêncio

05/09/2026: Claude (janela de 5h) e codex (cota até dia 7) caíram JUNTOS. A
Pessoa respondeu "Tive um probleminha técnico" 6x na linha da Corretor e nunca
voltou — um lead disse "Quero", outra pediu "apartamento 2 quartos acima de
70m²", e ninguém respondeu. Esta tabela guarda a mensagem do lead quando
nenhum provider responde; um worker retenta com backoff até um voltar.

Índice único PARCIAL (telefone) WHERE status='pendente': 1 item por lead —
mensagens seguidas se juntam no mesmo texto em vez de abrir N retomadas.

Migration 100% ADITIVA: cria uma tabela nova, nenhum ALTER em tabela existente.

Revision ID: 0078
Revises: 0077
Create Date: 2026-09-05
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0078"
down_revision: Union[str, None] = "0077"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pessoa_retomadas",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("telefone", sa.Text(), nullable=False),
        sa.Column("nome", sa.Text(), nullable=True),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("erro", sa.Text(), nullable=True),
        # pendente | feita | cancelada | desistiu
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pendente'")),
        sa.Column("tentativas", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("proxima_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resultado", sa.Text(), nullable=True),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "atualizado_em",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_pessoa_retomadas_status_proxima", "pessoa_retomadas", ["status", "proxima_em"]
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_pessoa_retomadas_pendente "
        "ON pessoa_retomadas (telefone) WHERE status = 'pendente'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_pessoa_retomadas_pendente")
    op.drop_index("ix_pessoa_retomadas_status_proxima", table_name="pessoa_retomadas")
    op.drop_table("pessoa_retomadas")
