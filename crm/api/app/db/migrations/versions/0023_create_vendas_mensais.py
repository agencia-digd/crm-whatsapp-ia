"""create vendas_mensais — agregado histórico Funil (Onda 8 Fase 8.6.1)

Revision ID: 0023
Revises: 0022
Create Date: 2026-05-12
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "vendas_mensais",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("corretora_id", sa.Integer(), sa.ForeignKey("corretoras.id"), nullable=False),
        sa.Column("ano_mes", sa.CHAR(7), nullable=False),
        sa.Column("qtde_vendas", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("vgv", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("qtde_leads", sa.Integer(), nullable=True),
        sa.Column("qtde_atendidos", sa.Integer(), nullable=True),
        sa.Column("qtde_agendados", sa.Integer(), nullable=True),
        sa.Column("qtde_visitas", sa.Integer(), nullable=True),
        sa.Column("fonte", sa.Text(), nullable=True),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("corretora_id", "ano_mes", name="uq_vendas_mensais_corretora_mes"),
        sa.CheckConstraint("qtde_vendas >= 0", name="ck_vendas_mensais_qtde_nonneg"),
        sa.CheckConstraint("vgv >= 0", name="ck_vendas_mensais_vgv_nonneg"),
    )
    op.create_index(
        "idx_vendas_mensais_corretora_mes",
        "vendas_mensais",
        ["corretora_id", sa.text("ano_mes DESC")],
    )


def downgrade() -> None:
    op.drop_index("idx_vendas_mensais_corretora_mes", table_name="vendas_mensais")
    op.drop_table("vendas_mensais")
