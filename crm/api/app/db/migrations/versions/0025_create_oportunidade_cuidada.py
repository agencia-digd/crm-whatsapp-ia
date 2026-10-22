"""oportunidade_cuidada — corretora marca lead como "já cuidei" no painel (Onda Inspector v2 · Fase 6)

Tabela leve usada pelo frontend /painel/atendimento. Quando a corretora
clica "Já cuidei" num card, registra (corretora_id, lead_id, marcado_em).
UNIQUE(corretora_id, lead_id) garante idempotência — repetir só atualiza
o timestamp.

Revision ID: 0025
Revises: 0024
Create Date: 2026-05-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "oportunidade_cuidada",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "corretora_id",
            sa.Integer,
            sa.ForeignKey("corretoras.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("lead_id", sa.Text, nullable=False),
        sa.Column(
            "marcado_em",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "corretora_id", "lead_id", name="uq_oportunidade_cuidada_corretora_lead"
        ),
    )
    op.create_index(
        "ix_oportunidade_cuidada_corretora",
        "oportunidade_cuidada",
        ["corretora_id", "marcado_em"],
    )


def downgrade() -> None:
    op.drop_index("ix_oportunidade_cuidada_corretora", table_name="oportunidade_cuidada")
    op.drop_table("oportunidade_cuidada")
