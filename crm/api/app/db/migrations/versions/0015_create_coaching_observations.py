"""create coaching_observations — Onda 3a.1

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CATEGORIAS = (
    "falta_qualificacao",
    "foco_valor_cedo",
    "resposta_seca",
    "perdeu_deixa",
    "demora_resposta",
    "ausencia_followup",
    "outro",
)

ORIGENS = ("lead_quente", "lead_esfriou")


def upgrade() -> None:
    op.create_table(
        "coaching_observations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("corretora_id", sa.BigInteger(),
                  sa.ForeignKey("corretoras.id"), nullable=False),
        sa.Column("lead_id", sa.BigInteger(), nullable=True),
        sa.Column("lead_nome", sa.Text(), nullable=True),
        sa.Column("trecho_corretora", sa.Text(), nullable=True),
        sa.Column("observacao", sa.Text(), nullable=False),
        sa.Column("sugestao", sa.Text(), nullable=True),
        sa.Column("categoria", sa.Text(), nullable=False),
        sa.Column("origem", sa.Text(), nullable=False),
        sa.Column("analyzed_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "categoria IN ('" + "','".join(CATEGORIAS) + "')",
            name="coaching_obs_categoria_chk",
        ),
        sa.CheckConstraint(
            "origem IN ('" + "','".join(ORIGENS) + "')",
            name="coaching_obs_origem_chk",
        ),
    )
    op.create_index(
        "idx_coaching_corretora_data",
        "coaching_observations",
        ["corretora_id", sa.text("analyzed_at DESC")],
    )
    op.create_index(
        "idx_coaching_categoria",
        "coaching_observations",
        ["categoria"],
    )


def downgrade() -> None:
    op.drop_index("idx_coaching_categoria", table_name="coaching_observations")
    op.drop_index("idx_coaching_corretora_data", table_name="coaching_observations")
    op.drop_table("coaching_observations")
