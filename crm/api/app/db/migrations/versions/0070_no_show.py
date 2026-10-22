"""no_show_toques: uma cobrança por falta, nunca duas

O UNIQUE é por `visita_id`: cada visita não comparecida rende no máximo um
toque, para sempre. Sem isso o tick repetiria a cobrança a cada rodada enquanto
a pessoa não respondesse — e cobrar falta duas vezes é o caminho mais curto pro
lead bloquear a corretora.

A ORDEM (1º ou 2º no-show da pessoa) fica gravada porque é o que decide o tom.
A cota de 3 do acervo se aplica por TELEFONE, contada em `visitas` — aqui só
guardamos o que já saiu.

Revision ID: 0070
Revises: 0069
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "no_show_toques",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("visita_id", sa.BigInteger, nullable=False),
        sa.Column("telefone", sa.Text, nullable=False),
        sa.Column("corretora_id", sa.Integer, nullable=True),
        sa.Column("ordem", sa.Integer, nullable=False),
        sa.Column("texto", sa.Text, nullable=True),
        sa.Column("enviado_em", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "visita_id", name="uq_no_show_visita"),
    )
    op.create_index("ix_no_show_enviado_em", "no_show_toques", ["enviado_em"])


def downgrade() -> None:
    op.drop_index("ix_no_show_enviado_em", table_name="no_show_toques")
    op.drop_table("no_show_toques")
