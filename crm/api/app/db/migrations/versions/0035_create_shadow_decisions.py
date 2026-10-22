"""create shadow_decisions — modo sombra do aprendiz (IA decide, não envia)

Para cada conversa onde o lead falou por último, a IA decide — SEM ENVIAR —
o que responderia, com confiança e bucket (responderia_sozinha | rascunharia |
chamaria_voce). Alimenta o placar "Aprendiz" por corretora e, na fase seguinte,
o aprendizado (comparar a decisão da IA com a resposta real da corretora).

Revision ID: 0035
Revises: 0034
Create Date: 2026-06-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shadow_decisions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("corretora_id", sa.Integer(), sa.ForeignKey("corretoras.id", ondelete="SET NULL"), nullable=True),
        sa.Column("lead_id", sa.Text(), nullable=True),
        sa.Column("lead_nome", sa.Text(), nullable=True),
        sa.Column("lead_telefone", sa.Text(), nullable=True),
        sa.Column("bucket", sa.Text(), nullable=False),
        sa.Column("confianca", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("resposta_sugerida", sa.Text(), nullable=True),
        sa.Column("motivo", sa.Text(), nullable=True),
        sa.Column("ultima_msg_lead", sa.Text(), nullable=True),
        sa.Column("ultima_msg_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("modelo", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        # fase aprendiz (preenchido depois, quando a corretora responde de verdade):
        sa.Column("resposta_real", sa.Text(), nullable=True),
        sa.Column("acerto", sa.Boolean(), nullable=True),
        sa.Column("avaliado_em", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "bucket IN ('responderia_sozinha','rascunharia','chamaria_voce')",
            name="ck_shadow_decisions_bucket",
        ),
        sa.CheckConstraint(
            "confianca >= 0 AND confianca <= 100",
            name="ck_shadow_decisions_confianca",
        ),
    )
    op.create_index("ix_shadow_decisions_cid", "shadow_decisions", ["corretora_id"])
    op.create_index("ix_shadow_decisions_lead", "shadow_decisions", ["lead_id"])
    op.create_index("ix_shadow_decisions_created", "shadow_decisions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_shadow_decisions_created", table_name="shadow_decisions")
    op.drop_index("ix_shadow_decisions_lead", table_name="shadow_decisions")
    op.drop_index("ix_shadow_decisions_cid", table_name="shadow_decisions")
    op.drop_table("shadow_decisions")
