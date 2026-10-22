"""snapshots: métricas CRM vs total (envios no funil vs qualquer 1:1)

A Dona pediu dois números no briefing: volume total no WhatsApp da corretora
E só o que bate com lead no CRM (leads_snapshot). Assim dá pra ver se alguém
está inflando com conversa fora do funil / pessoal na mesma linha.

Colunas novas (nullable default 0) em snapshots_corretora e snapshots_diarios.
Migration 100% ADITIVA.

Revision ID: 0067
Revises: 0066
Create Date: 2026-07-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0067"
down_revision: Union[str, None] = "0066"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "snapshots_corretora",
        sa.Column("envios_crm", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "snapshots_corretora",
        sa.Column("recebidos_crm", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "snapshots_corretora",
        sa.Column("contatos_crm", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "snapshots_corretora",
        sa.Column("responderam_crm", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "snapshots_corretora",
        sa.Column("taxa_resposta_crm", sa.Numeric(5, 2), nullable=True),
    )

    op.add_column(
        "snapshots_diarios",
        sa.Column("total_envios_crm", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "snapshots_diarios",
        sa.Column("total_recebidos_crm", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "snapshots_diarios",
        sa.Column("total_contatos_crm", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("snapshots_diarios", "total_contatos_crm")
    op.drop_column("snapshots_diarios", "total_recebidos_crm")
    op.drop_column("snapshots_diarios", "total_envios_crm")
    op.drop_column("snapshots_corretora", "taxa_resposta_crm")
    op.drop_column("snapshots_corretora", "responderam_crm")
    op.drop_column("snapshots_corretora", "contatos_crm")
    op.drop_column("snapshots_corretora", "recebidos_crm")
    op.drop_column("snapshots_corretora", "envios_crm")
