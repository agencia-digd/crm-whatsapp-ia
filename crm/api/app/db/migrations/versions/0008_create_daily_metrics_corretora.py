"""create daily_metrics_corretora — Onda P1

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "daily_metrics_corretora",
        sa.Column("data", sa.Date(), nullable=False),
        sa.Column("corretora_id", sa.Integer(), sa.ForeignKey("corretoras.id"), nullable=False),
        sa.Column("total_leads_mes", sa.Integer(), nullable=True),
        sa.Column("atendidos_mes", sa.Integer(), nullable=True),
        sa.Column("tarefas_atrasadas", sa.Integer(), nullable=True),
        sa.Column("fontes_completas", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("fontes_falhas", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("gerado_em", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("data", "corretora_id"),
    )
    op.create_index(
        "idx_daily_metrics_corretora",
        "daily_metrics_corretora",
        ["corretora_id", sa.text("data DESC")],
    )


def downgrade() -> None:
    op.drop_index("idx_daily_metrics_corretora", table_name="daily_metrics_corretora")
    op.drop_table("daily_metrics_corretora")
