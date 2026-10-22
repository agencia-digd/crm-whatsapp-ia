"""create assistente_conversas — histórico de interações da Assistente Copilota

Cada request do /painel/assistente e /painel/assistente/acao grava aqui (best-effort,
nunca quebra o fluxo). Serve pra medir adoção, intents mais usados, taxa de
sucesso das ações e latência. Gap 🔴 B1 da REVISAO-06-ASSISTENTE.

Revision ID: 0036
Revises: 0035
Create Date: 2026-07-02
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0038"
down_revision: Union[str, None] = "0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "assistente_conversas",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("corretora_id", sa.Integer(), sa.ForeignKey("corretoras.id", ondelete="SET NULL"), nullable=True),
        sa.Column("usuario", sa.Text(), nullable=False),
        sa.Column("intent", sa.Text(), nullable=True),
        sa.Column("pergunta", sa.Text(), nullable=True),
        sa.Column("resposta_resumo", sa.String(300), nullable=True),
        sa.Column("acao_tipo", sa.Text(), nullable=True),
        sa.Column("acao_executada", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("latencia_ms", sa.Integer(), nullable=True),
        sa.Column("criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_assistente_conversas_cid", "assistente_conversas", ["corretora_id"])
    op.create_index("ix_assistente_conversas_intent", "assistente_conversas", ["intent"])
    op.create_index("ix_assistente_conversas_criado", "assistente_conversas", ["criado_em"])


def downgrade() -> None:
    op.drop_index("ix_assistente_conversas_criado", table_name="assistente_conversas")
    op.drop_index("ix_assistente_conversas_intent", table_name="assistente_conversas")
    op.drop_index("ix_assistente_conversas_cid", table_name="assistente_conversas")
    op.drop_table("assistente_conversas")
