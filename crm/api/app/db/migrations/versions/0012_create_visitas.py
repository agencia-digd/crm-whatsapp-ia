"""create visitas — Onda P1

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "visitas",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("corretora_id", sa.Integer(), sa.ForeignKey("corretoras.id"), nullable=False),
        sa.Column("evento_calendar_id", sa.Text(), nullable=True, unique=True),
        sa.Column("lead_nome", sa.Text(), nullable=False),
        sa.Column("data_hora", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bairro", sa.Text(), nullable=True),
        sa.Column("telefone_lead", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="agendada"),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("criado_por_telefone", sa.Text(), nullable=True),
        sa.Column("status_atualizado_por_tel", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('agendada','compareceu','no_show','cancelada')",
            name="ck_visitas_status",
        ),
    )
    op.create_index("idx_visitas_corretora_data", "visitas",
                    ["corretora_id", sa.text("data_hora DESC")])
    op.create_index("idx_visitas_status", "visitas", ["status"])


def downgrade() -> None:
    op.drop_index("idx_visitas_status", table_name="visitas")
    op.drop_index("idx_visitas_corretora_data", table_name="visitas")
    op.drop_table("visitas")
