"""busca_imovel_jobs — busca de imóvel na web vira job assíncrono

A busca de imóvel de terceiros roda no imovel-fetcher do Mac mini (Playwright/
Chromium via Vpn) e leva dezenas de segundos. O endpoint era síncrono: a
corretora ficava presa na tela até o Mac responder. Esta tabela persiste o job
pra que o POST devolva um id na hora, o worker preencha o resultado em
background e a tela possa ser fechada/reaberta sem perder a busca.

Índice único PARCIAL (usuario, fingerprint) WHERE status='rodando' — é o
dedupe: a mesma corretora pedindo a MESMA busca que já está no ar reaproveita o
job em vez de abrir outra sessão de Chromium no Mac.

Migration 100% ADITIVA: cria uma tabela nova, nenhum ALTER em tabela existente.

Revision ID: 0064
Revises: 0063
Create Date: 2026-07-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0064"
down_revision: Union[str, None] = "0063"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "busca_imovel_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("usuario", sa.Text(), nullable=False),
        sa.Column("corretora_id", sa.Integer(), nullable=True),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        # rodando | pronto | erro
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'rodando'")),
        sa.Column("resultado", JSONB(), nullable=True),
        sa.Column("erro", sa.Text(), nullable=True),
        sa.Column(
            "criado_em", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "heartbeat_em",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("concluido_em", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_busca_imovel_jobs_usuario_criado", "busca_imovel_jobs", ["usuario", "criado_em"]
    )
    # Dedupe: no máximo 1 job RODANDO por (usuário, critérios).
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_busca_imovel_jobs_rodando "
        "ON busca_imovel_jobs (usuario, fingerprint) WHERE status = 'rodando'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_busca_imovel_jobs_rodando")
    op.drop_index("ix_busca_imovel_jobs_usuario_criado", table_name="busca_imovel_jobs")
    op.drop_table("busca_imovel_jobs")
