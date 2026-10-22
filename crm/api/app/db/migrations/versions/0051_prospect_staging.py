"""staging de prospecção (F0 SDR agencia) — prospect_searches + prospect_leads + seed "Prospectado"

Módulo Prospecção/SDR do dash agencia (spec: projetos/agencia-central/docs/
SPEC-PROSPECT-STAGING.md · plano: PLANO-PROSPECCAO-SDR.md F0). Duas tabelas de
STAGING no banco copiloto (mesmo do CRM canônico tenant 2 = agencia):

  - `prospect_searches` — 1 linha por busca em linguagem natural criada no dash;
    o runner (Xpoz via claude -p) faz claim (pendente→processando) e grava o
    custo REAL em créditos ao concluir.
  - `prospect_leads`  — perfis descobertos/qualificados/enriquecidos ANTES de
    virarem lead no CRM. UNIQUE (tenant_id, username) = alvo do ON CONFLICT do
    runner (re-rodar busca não duplica).

Seed: estágio "Prospectado" no pipeline tenant 2 (funil 'principal'), ordem -1
— fica ANTES de "Novo" (ordem 0) SEM update nas linhas existentes (aditivo
puro). Idempotente: só insere se não existir.

Migration 100% ADITIVA: nenhum ALTER em tabela existente.
⚠️ NÃO aplicar no banco vivo sem pg_dump antes (processo da Assistente).

Revision ID: 0051
Revises: 0050
Create Date: 2026-07-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0051"
down_revision: Union[str, None] = "0050"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TENANT_AGENCIA = 2
_STAGE_PROSPECTADO = "Prospectado"


def upgrade() -> None:
    op.create_table(
        "prospect_searches",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False, server_default=sa.text("2")),
        sa.Column("descricao_natural", sa.Text(), nullable=False),
        sa.Column("params_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        # pendente → processando → concluida | erro
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pendente'")),
        # créditos Xpoz REAIS medidos pelo runner (diff do getCreditsUsageHistory)
        sa.Column("custo_creditos", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("erro", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_prospect_searches_tenant_status", "prospect_searches", ["tenant_id", "status"]
    )

    op.create_table(
        "prospect_leads",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "search_id",
            sa.BigInteger(),
            sa.ForeignKey("prospect_searches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Integer(), nullable=False, server_default=sa.text("2")),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("nome", sa.Text(), nullable=True),
        sa.Column("bio", sa.Text(), nullable=True),
        sa.Column("link", sa.Text(), nullable=True),
        sa.Column("telefone_whatsapp", sa.Text(), nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("qualificacao_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        # staging | promovido | descartado — NUNCA regride (regra do upsert do runner)
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'staging'")),
        sa.Column("crm_lead_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "username", name="uq_prospect_leads_tenant_username"),
    )
    op.create_index("ix_prospect_leads_search", "prospect_leads", ["search_id"])

    # Seed idempotente do estágio "Prospectado" (tenant agencia, funil principal).
    conn = op.get_bind()
    existe = conn.execute(
        sa.text(
            "SELECT 1 FROM pipeline_stages "
            "WHERE tenant_id = :t AND funil = 'principal' AND nome = :n"
        ),
        {"t": _TENANT_AGENCIA, "n": _STAGE_PROSPECTADO},
    ).first()
    if existe is None:
        conn.execute(
            sa.text(
                "INSERT INTO pipeline_stages (tenant_id, nome, ordem, funil) "
                "VALUES (:t, :n, -1, 'principal')"
            ),
            {"t": _TENANT_AGENCIA, "n": _STAGE_PROSPECTADO},
        )


def downgrade() -> None:
    conn = op.get_bind()
    op.drop_index("ix_prospect_leads_search", table_name="prospect_leads")
    op.drop_table("prospect_leads")
    op.drop_index("ix_prospect_searches_tenant_status", table_name="prospect_searches")
    op.drop_table("prospect_searches")
    # remove o seed só se nenhum lead estiver pendurado nele
    conn.execute(
        sa.text(
            "DELETE FROM pipeline_stages "
            "WHERE tenant_id = :t AND funil = 'principal' AND nome = :n "
            "AND NOT EXISTS (SELECT 1 FROM leads WHERE leads.stage_id = pipeline_stages.id)"
        ),
        {"t": _TENANT_AGENCIA, "n": _STAGE_PROSPECTADO},
    )
