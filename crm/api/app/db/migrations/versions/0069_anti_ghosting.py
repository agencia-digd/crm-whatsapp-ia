"""anti_ghosting_toques: dedup do reabre-conversa dentro da janela de 24h

O anti-ghosting fala com quem ESTAVA conversando e parou no meio — janela de
24h ainda aberta, então sai texto livre, sem template e sem custo. É a
automação mais barata da casa.

A tabela existe só pra garantir UMA coisa: cada toque sai no máximo uma vez por
conversa parada. Sem ela, o tick de 10 em 10 minutos mandaria "👀" a cada
rodada enquanto a pessoa não respondesse — que é exatamente a diferença entre
reabrir uma conversa e perseguir alguém.

A chave inclui `desde` (o timestamp da última mensagem do lead): é o que separa
uma parada de outra. Se a pessoa responder e sumir de novo semana que vem, é
uma conversa nova parada e o toque pode sair outra vez.

Revision ID: 0069
Revises: 0068
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "anti_ghosting_toques",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("telefone", sa.Text, nullable=False),
        sa.Column("corretora_id", sa.Integer, nullable=True),
        sa.Column("toque", sa.Integer, nullable=False),
        # `desde` = timestamp da última mensagem do lead que originou a parada.
        # É o que faz cada silêncio ser um evento próprio.
        sa.Column("desde", sa.DateTime(timezone=True), nullable=False),
        sa.Column("enviado_em", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("texto", sa.Text, nullable=True),
        sa.UniqueConstraint("tenant_id", "telefone", "desde", "toque",
                            name="uq_anti_ghosting_toque"),
    )
    op.create_index(
        "ix_anti_ghosting_enviado_em", "anti_ghosting_toques", ["enviado_em"]
    )


def downgrade() -> None:
    op.drop_index("ix_anti_ghosting_enviado_em", table_name="anti_ghosting_toques")
    op.drop_table("anti_ghosting_toques")
