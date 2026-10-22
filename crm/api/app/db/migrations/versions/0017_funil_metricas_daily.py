"""add funil metrics to daily_metrics_corretora — Onda P2.2

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-10
"""
import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("daily_metrics_corretora",
                  sa.Column("entrou_mes", sa.Integer(), nullable=True))
    op.add_column("daily_metrics_corretora",
                  sa.Column("em_atendimento_mes", sa.Integer(), nullable=True))
    op.add_column("daily_metrics_corretora",
                  sa.Column("agendados_mes", sa.Integer(), nullable=True))
    op.add_column("daily_metrics_corretora",
                  sa.Column("visitas_realizadas_mes", sa.Integer(), nullable=True))
    op.add_column("daily_metrics_corretora",
                  sa.Column("vendas_kommo_mes", sa.Integer(), nullable=True))
    op.add_column("daily_metrics_corretora",
                  sa.Column("vgv_kommo_mes", sa.Numeric(14, 2), nullable=True))

    op.execute("""
        COMMENT ON COLUMN daily_metrics_corretora.entrou_mes IS
        'Leads distintos que passaram pela etapa entrou (10000009) no mês,
         incluindo reativações. Fonte: events lead_status_changed.';

        COMMENT ON COLUMN daily_metrics_corretora.em_atendimento_mes IS
        'Leads distintos em etapa em_atendimento (10000005) no mês. Fonte: events.';

        COMMENT ON COLUMN daily_metrics_corretora.agendados_mes IS
        'Leads distintos que chegaram a visita_agendada (10000002) no mês. Fonte: events.';

        COMMENT ON COLUMN daily_metrics_corretora.visitas_realizadas_mes IS
        'Leads distintos que chegaram a visita_realizada (10000012) no mês. Fonte: events.';

        COMMENT ON COLUMN daily_metrics_corretora.vendas_kommo_mes IS
        'Leads distintos que chegaram a venda_ganha (142) no mês. Fonte: events.';

        COMMENT ON COLUMN daily_metrics_corretora.vgv_kommo_mes IS
        'Soma de lead.price dos leads que chegaram a venda_ganha no mês.
         Pode ser 0 se corretora nao preenche price. Risco documentado.';
    """)


def downgrade() -> None:
    for col in (
        "vgv_kommo_mes", "vendas_kommo_mes", "visitas_realizadas_mes",
        "agendados_mes", "em_atendimento_mes", "entrou_mes",
    ):
        op.drop_column("daily_metrics_corretora", col)
