"""create vendas — Onda P1

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "vendas",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("corretora_id", sa.Integer(), sa.ForeignKey("corretoras.id"), nullable=False),
        sa.Column("cliente_nome", sa.Text(), nullable=False),
        sa.Column("valor", sa.Numeric(14, 2), nullable=False),
        sa.Column("data_venda", sa.Date(), nullable=False),
        sa.Column("observacao", sa.Text(), nullable=True),
        sa.Column("registrado_por_telefone", sa.Text(), nullable=True),
        sa.Column("registrado_em", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("valor >= 0", name="ck_vendas_valor_nonneg"),
    )
    op.create_index("idx_vendas_corretora_data", "vendas",
                    ["corretora_id", sa.text("data_venda DESC")])
    op.create_index("idx_vendas_data", "vendas", [sa.text("data_venda DESC")])


def downgrade() -> None:
    op.drop_index("idx_vendas_data", table_name="vendas")
    op.drop_index("idx_vendas_corretora_data", table_name="vendas")
    op.drop_table("vendas")
