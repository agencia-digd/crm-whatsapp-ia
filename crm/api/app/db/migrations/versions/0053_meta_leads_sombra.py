"""meta_leads_sombra — auditoria do receptor Meta leadgen em MODO SOMBRA (B6)

Cada evento leadgen da Meta que chegar no receptor próprio do copiloto
(POST /intake/meta-leadgen/{secret}) vira UMA linha aqui, SEMPRE — mesmo com
LEAD_INTAKE_ATIVO=false. É a prova de que o cano direto Meta→copiloto funciona
antes de virar a chave e largar o Kommo na entrada.

Idempotência por leadgen_id UNIQUE (mesmo padrão do meta_leads_log do
automacao-kommo). `processado=true` só quando o lead realmente entrou no fluxo
/intake/lead (flag ligada); em sombra fica false.

Migration 100% ADITIVA: nenhum ALTER em tabela existente.

Revision ID: 0053
Revises: 0052
Create Date: 2026-07-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0053"
down_revision: Union[str, None] = "0052"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "meta_leads_sombra",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("leadgen_id", sa.Text(), nullable=False),
        sa.Column("nome", sa.Text(), nullable=True),
        sa.Column("telefone", sa.Text(), nullable=True),
        # campaign_name da Graph API — a futura tag rica 📣 do card do CRM
        sa.Column("campanha", sa.Text(), nullable=True),
        sa.Column("ad_id", sa.Text(), nullable=True),
        sa.Column("form_id", sa.Text(), nullable=True),
        # webhook value + resposta da Graph (field_data etc.) — auditoria completa
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "recebido_em",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("processado", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.UniqueConstraint("leadgen_id", name="uq_meta_leads_sombra_leadgen_id"),
    )


def downgrade() -> None:
    op.drop_table("meta_leads_sombra")
