"""create atendimentos_evolution_mensais — atendidos calculados a partir do WhatsApp real

Revision ID: 0028
Revises: 0027
Create Date: 2026-05-20

Tabela paralela a vendas_mensais.qtde_atendidos. A diferença: aqui "atendido"
significa lead novo (criado no mês) com conversa 2-via real na Evolution API
— cobre @s.whatsapp.net + @lid. Não depende da corretora mover etapa no Kommo.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "atendimentos_evolution_mensais",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("corretora_id", sa.Integer(), sa.ForeignKey("corretoras.id"), nullable=False),
        sa.Column("ano_mes", sa.CHAR(7), nullable=False),
        sa.Column("leads_novos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("atendidos_2via", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("so_corretora_falou", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sem_msg", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tel_invalido", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("corretora_id", "ano_mes", name="uq_atend_evo_corretora_mes"),
        sa.CheckConstraint("atendidos_2via >= 0", name="ck_atend_evo_nonneg"),
        sa.CheckConstraint("atendidos_2via <= leads_novos", name="ck_atend_evo_le_leads"),
    )
    op.create_index(
        "idx_atend_evo_corretora_mes",
        "atendimentos_evolution_mensais",
        ["corretora_id", sa.text("ano_mes DESC")],
    )


def downgrade() -> None:
    op.drop_index("idx_atend_evo_corretora_mes", table_name="atendimentos_evolution_mensais")
    op.drop_table("atendimentos_evolution_mensais")
