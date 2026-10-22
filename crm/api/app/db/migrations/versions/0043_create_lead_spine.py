"""cria o schema canônico do lead (Opção A) — leads / pipeline_stages / lead_events

Decisão de arquitetura (Opção A): o nosso Postgres vira o DONO da verdade do lead.
Hoje o lead vive no Kommo e o copiloto só tem `leads_snapshot` (cache).

Esta migration cria a espinha canônica MULTI-TENANT (compartilhada com a plataforma
agencia): `pipeline_stages`, `leads`, `lead_events`. NÃO altera o comportamento de
leitura atual — é o alvo do dual-write EM SOMBRA (shadow), reversível via a flag
`LEAD_SPINE_SHADOW` (settings.lead_spine_shadow). Enquanto o shadow estiver ligado,
o Kommo continua sendo a fonte de leitura; só gravamos em paralelo pra validar.

Contrato de schema (nomes/campos EXATOS — compartilhado com a plataforma agencia):

    pipeline_stages(id, tenant_id, nome, ordem, tipo)
    leads(id, tenant_id, external_ref, nome, telefone, owner_id, stage_id,
          origem, valor, created_at, updated_at, UNIQUE(tenant_id, external_ref))
    lead_events(id, tenant_id, lead_id, tipo, payload, at)

Índices: (tenant_id, external_ref) [via UNIQUE] e (tenant_id, stage_id).

Revision ID: 0043
Revises: 0042
Create Date: 2026-07-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "0043"
down_revision: Union[str, None] = "0042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pipeline_stages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("nome", sa.Text(), nullable=True),
        sa.Column("ordem", sa.Integer(), nullable=True),
        sa.Column("tipo", sa.Text(), nullable=True),
    )

    op.create_table(
        "leads",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("external_ref", sa.Text(), nullable=True),
        sa.Column("nome", sa.Text(), nullable=True),
        sa.Column("telefone", sa.Text(), nullable=True),
        sa.Column("owner_id", sa.Integer(), nullable=True),
        sa.Column(
            "stage_id",
            sa.BigInteger(),
            sa.ForeignKey("pipeline_stages.id"),
            nullable=True,
        ),
        sa.Column("origem", sa.Text(), nullable=True),
        # Valor de negócio do lead (R$). Alimenta os KPIs de R$ do CRM — sem ele
        # ficam zerados. Vem do campo `price` do lead no Kommo (shadow).
        sa.Column("valor", sa.Numeric(12, 2), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "external_ref", name="uq_leads_tenant_external_ref"),
    )
    op.create_index("ix_leads_tenant_stage", "leads", ["tenant_id", "stage_id"])

    op.create_table(
        "lead_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("lead_id", sa.BigInteger(), sa.ForeignKey("leads.id"), nullable=True),
        sa.Column("tipo", sa.Text(), nullable=True),
        sa.Column("payload", JSONB(), nullable=True),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("lead_events")
    op.drop_index("ix_leads_tenant_stage", table_name="leads")
    op.drop_table("leads")
    op.drop_table("pipeline_stages")
