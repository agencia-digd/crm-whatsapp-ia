"""create lead_handoff_notes — narrativa curada de passagem de lead pra Assistente

Revision ID: 0032
Revises: 0031
Create Date: 2026-06-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "lead_handoff_notes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("telefone", sa.Text(), nullable=False),
        sa.Column("lead_nome", sa.Text(), nullable=True),
        sa.Column("corretora_id", sa.Integer(), sa.ForeignKey("corretoras.id", ondelete="SET NULL"), nullable=True),
        sa.Column("contexto", sa.Text(), nullable=False),
        sa.Column("origem", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'ativo'")),
        sa.Column("criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('ativo','arquivado')",
            name="ck_lead_handoff_notes_status",
        ),
    )
    op.create_index("ix_lead_handoff_notes_telefone", "lead_handoff_notes", ["telefone"])


def downgrade() -> None:
    op.drop_index("ix_lead_handoff_notes_telefone", table_name="lead_handoff_notes")
    op.drop_table("lead_handoff_notes")
