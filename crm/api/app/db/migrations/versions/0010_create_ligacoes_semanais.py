"""create ligacoes_semanais — Onda P1

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ligacoes_semanais",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("corretora_id", sa.Integer(), sa.ForeignKey("corretoras.id"), nullable=False),
        sa.Column("ano_semana_iso", sa.Text(), nullable=False),
        sa.Column("ligacoes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("atendeu", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prosseguiu", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("registrado_por_telefone", sa.Text(), nullable=True),
        sa.Column("registrado_em", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("corretora_id", "ano_semana_iso", name="uq_ligacoes_corretora_semana"),
        sa.CheckConstraint("atendeu <= ligacoes", name="ck_ligacoes_atendeu_le_ligacoes"),
        sa.CheckConstraint("prosseguiu <= atendeu", name="ck_ligacoes_pross_le_atendeu"),
    )
    op.create_index("idx_ligacoes_semana", "ligacoes_semanais", ["ano_semana_iso"])


def downgrade() -> None:
    op.drop_index("idx_ligacoes_semana", table_name="ligacoes_semanais")
    op.drop_table("ligacoes_semanais")
