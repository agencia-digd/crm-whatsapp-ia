"""add tenant_id às tabelas core — passo 1 do multi-tenant (Opção A)

Decisão de arquitetura (Opção A): o nosso Postgres é o dono da verdade do lead e
o produto vira SaaS multi-tenant. Primeiro passo (barato, destrava tudo): adicionar
`tenant_id` INTEGER NOT NULL nas tabelas CORE enquanto o schema é pequeno.

Todas as linhas existentes são da Acme → server_default = 1 (tenant Acme).
NÃO faz enforcement de tenant nas queries (isso é P1) — só cria a coluna + índices.

Índice em tenant_id só nas tabelas grandes (mensagens_whatsapp, lead_analyses).
Config global (stage_map), deprecated (internal_contacts), prospecção própria da agencia
(hunter_*) e tabelas do Agno (sessions/traces) ficam de fora.

Revision ID: 0042
Revises: 0041
Create Date: 2026-07-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0042"
down_revision: Union[str, None] = "0041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tenant default pra todas as linhas existentes = Acme (id 1).
_ACME_TENANT = "1"

# Tabelas CORE que carregam dado de negócio por imobiliária.
_CORE_TABLES = [
    "corretoras",
    "leads_snapshot",
    "lead_analyses",
    "briefings_sent",
    "patterns_weekly",
    "coaching_observations",
    "mensagens_automacao",
    "inspector_runs",
    "oportunidade_cuidada",
    "lembretes",
    "mensagens_whatsapp",
    "recovery_conversations",
    "recovery_messages",
    "lead_handoff_notes",
    "tarefas_corretora",
    "assistente_conversas",
    "disparos_reengajamento",
    "shadow_decisions",
    "internos_acme",
    "visitas",
    "vendas",
    "vendas_mensais",
    "daily_metrics_corretora",
    "ligacoes_semanais",
    "agendamentos_semanais",
    "notas",
    "atendimentos_evolution_mensais",
    "observacao_corretora",
    "conversations",
]

# Tabelas grandes que ganham índice em tenant_id (queries filtram por tenant em P1).
_INDEXED_TABLES = [
    "mensagens_whatsapp",
    "lead_analyses",
]


def upgrade() -> None:
    for table in _CORE_TABLES:
        op.add_column(
            table,
            sa.Column(
                "tenant_id",
                sa.Integer(),
                nullable=False,
                server_default=sa.text(_ACME_TENANT),
            ),
        )

    for table in _INDEXED_TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])


def downgrade() -> None:
    for table in reversed(_INDEXED_TABLES):
        op.drop_index(f"ix_{table}_tenant_id", table_name=table)

    for table in reversed(_CORE_TABLES):
        op.drop_column(table, "tenant_id")
