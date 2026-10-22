"""add reengajados columns to atendimentos_evolution_mensais

Revision ID: 0029
Revises: 0028
Create Date: 2026-05-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Relaxa o constraint atendidos_2via <= leads_novos (agora reengajados contam à parte)
    op.drop_constraint("ck_atend_evo_le_leads", "atendimentos_evolution_mensais", type_="check")

    op.add_column(
        "atendimentos_evolution_mensais",
        sa.Column("reengajados_total", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "atendimentos_evolution_mensais",
        sa.Column("reengajados_2via", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_atend_evo_reeng_nonneg",
        "atendimentos_evolution_mensais",
        "reengajados_2via >= 0 AND reengajados_total >= 0 AND reengajados_2via <= reengajados_total",
    )


def downgrade() -> None:
    op.drop_constraint("ck_atend_evo_reeng_nonneg", "atendimentos_evolution_mensais", type_="check")
    op.drop_column("atendimentos_evolution_mensais", "reengajados_2via")
    op.drop_column("atendimentos_evolution_mensais", "reengajados_total")
    op.create_check_constraint(
        "ck_atend_evo_le_leads",
        "atendimentos_evolution_mensais",
        "atendidos_2via <= leads_novos",
    )
