"""create observacao_corretora — anotação livre da corretora por lead

A corretora escreve uma observação no card do painel (ex: "já falei, não é
morno, está com nome sujo"). A IA (Haiku) lê, entende, escreve uma nota limpa
no Kommo e pode reclassificar / descartar o lead.

Revision ID: 0033
Revises: 0032
Create Date: 2026-06-18
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "observacao_corretora",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("lead_id", sa.Text(), nullable=False),
        sa.Column("corretora_id", sa.Integer(), sa.ForeignKey("corretoras.id", ondelete="SET NULL"), nullable=True),
        sa.Column("autor", sa.Text(), nullable=True),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("ia_nota_kommo", sa.Text(), nullable=True),
        sa.Column("ia_temperatura", sa.Text(), nullable=True),
        sa.Column("ia_descartar", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("ia_motivo", sa.Text(), nullable=True),
        sa.Column("kommo_note_ok", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "ia_temperatura IS NULL OR ia_temperatura IN ('frio','morno','quente')",
            name="ck_observacao_corretora_temperatura",
        ),
    )
    op.create_index("ix_observacao_corretora_lead", "observacao_corretora", ["lead_id"])
    op.create_index("ix_observacao_corretora_cid_lead", "observacao_corretora", ["corretora_id", "lead_id"])


def downgrade() -> None:
    op.drop_index("ix_observacao_corretora_cid_lead", table_name="observacao_corretora")
    op.drop_index("ix_observacao_corretora_lead", table_name="observacao_corretora")
    op.drop_table("observacao_corretora")
