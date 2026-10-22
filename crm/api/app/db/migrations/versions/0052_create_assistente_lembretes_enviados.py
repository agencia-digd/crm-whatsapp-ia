"""assistente_lembretes_enviados — dedup do lembrete de pendência de registro

Marcador de cobrança do lembrete de pendência da Assistente (incidente Pessoa10/
Guilherme 22/07/2026: venda de R$ 423k ficou 2 dias fora do painel porque a
corretora não respondeu as infos que a Assistente pediu). Uma pendência é
identificada por (corretora_id, conversa_ultima_msg_at) — UNIQUE garante que
a mesma pendência nunca é cobrada 2x. Linha só é gravada em envio REAL.

Migration 100% ADITIVA: nenhum ALTER em tabela existente.

Revision ID: 0052
Revises: 0051
Create Date: 2026-07-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0052"
down_revision: Union[str, None] = "0051"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "assistente_lembretes_enviados",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "corretora_id",
            sa.Integer(),
            sa.ForeignKey("corretoras.id"),
            nullable=False,
        ),
        # created_at da ÚLTIMA mensagem da thread quando a pendência foi cobrada
        sa.Column("conversa_ultima_msg_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "enviado_em",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "corretora_id",
            "conversa_ultima_msg_at",
            name="uq_assistente_lembrete_pendencia",
        ),
    )


def downgrade() -> None:
    op.drop_table("assistente_lembretes_enviados")
