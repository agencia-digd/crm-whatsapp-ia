"""fila de DM Instagram (F3 SDR agencia) — prospect_dms

Módulo Prospecção/SDR do dash agencia (plano: PLANO-PROSPECCAO-SDR.md F3). UMA
tabela de fila pro DM-SENDER (envio de DM na conta @agencia, operado no Mac via
Playwright/CDP). Cada linha = 1 abordagem por DM a um lead:

  fila → aprovado → enviado → respondido        (caminho feliz)
                  ↘ falhou

- `status='aprovado'` é a única condição pro DM-SENDER processar.
- `aprovado_real=true` é a trava do PRIMEIRO envio real: mesmo com DRY_RUN=false
  o sender só clica "enviar" se este flag estiver setado por item (a Assistente, em
  sessão, libera item a item). Default false = só compõe o draft.
- `respondido` + `resposta_texto` são gravados pelo INBOX-READER (match por
  username); `notificado_at` marca que a Assistente já foi avisada.

Conta @agencia é PRINCIPAL (decisão da Dona, com guard-rails): a sensibilidade
mora no código do sender (caps duros, anti-detecção, kill-switch), não no schema.

Migration 100% ADITIVA: nenhum ALTER em tabela existente. Backup em
/home/assistente/backups/copiloto-pre-0052-*.sql (pg_dump antes de aplicar).

Revision ID: 0054
Revises: 0053
Create Date: 2026-07-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0054"
down_revision: Union[str, None] = "0053"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "prospect_dms",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False, server_default=sa.text("2")),
        sa.Column(
            "prospect_lead_id",
            sa.BigInteger(),
            sa.ForeignKey("prospect_leads.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("lead_username", sa.Text(), nullable=False),
        sa.Column("texto_dm", sa.Text(), nullable=False),
        # fila | aprovado | enviado | falhou | respondido
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'fila'")),
        # trava do 1º envio REAL: só a Assistente seta, item a item (nunca global)
        sa.Column("aprovado_real", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        # o sender marca true quando compôs o draft com sucesso em DRY_RUN
        sa.Column("dry_run_ok", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("screenshot_path", sa.Text(), nullable=True),
        sa.Column("enviado_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resposta_texto", sa.Text(), nullable=True),
        sa.Column("respondido_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notificado_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("erro", sa.Text(), nullable=True),
        sa.Column("tentativas", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("meta_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_prospect_dms_tenant_status", "prospect_dms", ["tenant_id", "status"])
    op.create_index("ix_prospect_dms_username", "prospect_dms", ["tenant_id", "lead_username"])


def downgrade() -> None:
    op.drop_index("ix_prospect_dms_username", table_name="prospect_dms")
    op.drop_index("ix_prospect_dms_tenant_status", table_name="prospect_dms")
    op.drop_table("prospect_dms")
