"""create agendamentos_semanais — Onda P1

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agendamentos_semanais",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("corretora_id", sa.Integer(), sa.ForeignKey("corretoras.id"), nullable=False),
        sa.Column("ano_semana_iso", sa.Text(), nullable=False),
        sa.Column("agendados", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("realizados", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("registrado_por_telefone", sa.Text(), nullable=True),
        sa.Column("registrado_em", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("corretora_id", "ano_semana_iso", name="uq_agendamentos_corretora_semana"),
        sa.CheckConstraint("realizados <= agendados", name="ck_agendamentos_real_le_agend"),
    )
    op.create_index("idx_agendamentos_semana", "agendamentos_semanais", ["ano_semana_iso"])


def downgrade() -> None:
    op.drop_index("idx_agendamentos_semana", table_name="agendamentos_semanais")
    op.drop_table("agendamentos_semanais")
