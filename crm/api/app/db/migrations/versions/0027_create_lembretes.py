"""create lembretes — Assistente grava + cron 1min dispara via WhatsApp

Revision ID: 0027
Revises: 0026
Create Date: 2026-05-17
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "lembretes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("corretora_id", sa.Integer(), sa.ForeignKey("corretoras.id", ondelete="SET NULL"), nullable=True),
        sa.Column("telefone_destino", sa.Text(), nullable=False),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("lembrar_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("criado_em", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("criado_via_msg_id", sa.Text(), nullable=True),
        sa.Column("enviado_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'pendente'"),
        ),
        sa.Column("erro", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pendente','enviado','cancelado','erro')",
            name="ck_lembretes_status",
        ),
    )
    op.create_index(
        "ix_lembretes_pendentes_vencidos",
        "lembretes",
        ["lembrar_em"],
        postgresql_where=sa.text("status = 'pendente'"),
    )


def downgrade() -> None:
    op.drop_index("ix_lembretes_pendentes_vencidos", table_name="lembretes")
    op.drop_table("lembretes")
