"""shadow_cadencia — sombra da cadência: o que a IA escreveria no lugar do template

A cadência de follow-up hoje é mala-direta: no toque N ela pega
`_TEMPLATES_MSG[N]`, texto fixo, e envia. O sistema SABE tudo sobre aquele lead
(o Inspector leu a conversa inteira, classificou, achou o gap) — e mesmo assim
manda texto genérico. A inteligência está toda na análise, nenhuma na ação.

Esta tabela é o experimento que mede se vale mudar isso: pra cada toque que
VENCERIA, a IA lê a conversa real e escreve o que ela mandaria. Grava os dois
lado a lado. NUNCA envia — quem julga é a Dona, comparando os pares.

Se a versão da IA ganhar de forma consistente, aí sim se discute A/B real.

Migration 100% ADITIVA: cria tabela nova, nenhum ALTER em tabela existente.

Revision ID: 0065
Revises: 0064
Create Date: 2026-07-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0065"
down_revision: Union[str, None] = "0064"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shadow_cadencia",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("corretora_id", sa.Integer(), sa.ForeignKey("corretoras.id"), nullable=True),
        sa.Column("lead_telefone", sa.Text(), nullable=True),
        sa.Column("lead_nome", sa.Text(), nullable=True),
        # qual toque da cadência venceria
        sa.Column("toque", sa.Integer(), nullable=False),
        # o que o sistema mandaria HOJE (template fixo, já com o nome preenchido)
        sa.Column("template_atual", sa.Text(), nullable=False),
        # o que a IA mandaria, tendo lido a conversa
        sa.Column("sugestao_ia", sa.Text(), nullable=True),
        # a IA achou algo na conversa que justifique fugir do template?
        sa.Column("usou_contexto", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        # o fato concreto da conversa que ela usou (pra Dona auditar se é real)
        sa.Column("contexto_usado", sa.Text(), nullable=True),
        sa.Column("motivo", sa.Text(), nullable=True),
        # veredito humano: null = não julgado; 'ia' | 'template' | 'empate'
        sa.Column("veredito", sa.Text(), nullable=True),
        sa.Column("modelo", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # o placar do experimento é lido por (corretora, data) e por veredito
    op.create_index(
        "ix_shadow_cadencia_corretora_data",
        "shadow_cadencia",
        ["corretora_id", "created_at"],
    )
    op.create_index(
        "ix_shadow_cadencia_veredito",
        "shadow_cadencia",
        ["veredito"],
    )


def downgrade() -> None:
    op.drop_index("ix_shadow_cadencia_veredito", table_name="shadow_cadencia")
    op.drop_index("ix_shadow_cadencia_corretora_data", table_name="shadow_cadencia")
    op.drop_table("shadow_cadencia")
