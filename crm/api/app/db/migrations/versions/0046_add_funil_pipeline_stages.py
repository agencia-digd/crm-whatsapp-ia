"""adiciona coluna `funil` em pipeline_stages + seed do funil REMARKETING

Segundo funil do CRM 2.0 (spec Dona 10/07): o board /remarketing do acme-painel
mostra os leads que ESGOTARAM a cadência de 8 toques, em 6 etapas de nutrição:

    Base → 30 dias → 60 dias → 90 dias → 120 dias → 150 dias

Pra suportar 2 funis na MESMA tabela `pipeline_stages` (contrato compartilhado
com a plataforma agencia), esta migration:

  1. adiciona `funil` (text, NOT NULL, default 'principal') — todas as etapas
     existentes viram automaticamente do funil principal, zero backfill;
  2. semeia as 6 etapas do funil 'remarketing' pro tenant 1 (Acme), ordem 1..6.
     Seed idempotente: só insere o que não existe (rodar 2x não duplica).

Boards: /painel/crm/board passa a filtrar funil='principal';
/painel/crm/remarketing/board (novo) lê funil='remarketing'.
Motor: app/services/nutricao.py (anexado ao tick da cadência).

⚠️ NÃO aplicar no banco vivo sem pg_dump antes (processo da Assistente).
Aplicar ANTES de reiniciar o copiloto-api — o model PipelineStage passa a
referenciar a coluna `funil` e o board quebra se o binário novo subir com o
schema antigo.

Revision ID: 0046
Revises: 0045
Create Date: 2026-07-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0046"
down_revision: Union[str, None] = "0045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Contrato do funil de nutrição — mesmos nomes em app/services/nutricao.py.
_TENANT_ACME = 1
_ETAPAS_REMARKETING: tuple[str, ...] = (
    "Base", "30 dias", "60 dias", "90 dias", "120 dias", "150 dias"
)


def upgrade() -> None:
    op.add_column(
        "pipeline_stages",
        sa.Column(
            "funil",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'principal'"),
        ),
    )

    # Seed idempotente das 6 etapas do funil remarketing (tenant Acme).
    conn = op.get_bind()
    for ordem, nome in enumerate(_ETAPAS_REMARKETING, start=1):
        existe = conn.execute(
            sa.text(
                "SELECT 1 FROM pipeline_stages "
                "WHERE tenant_id = :t AND funil = 'remarketing' AND nome = :n"
            ),
            {"t": _TENANT_ACME, "n": nome},
        ).first()
        if existe is None:
            conn.execute(
                sa.text(
                    "INSERT INTO pipeline_stages (tenant_id, nome, ordem, funil) "
                    "VALUES (:t, :n, :o, 'remarketing')"
                ),
                {"t": _TENANT_ACME, "n": nome, "o": ordem},
            )


def downgrade() -> None:
    conn = op.get_bind()
    # solta os leads das etapas de remarketing antes de removê-las (FK leads.stage_id)
    conn.execute(
        sa.text(
            "UPDATE leads SET stage_id = NULL WHERE stage_id IN "
            "(SELECT id FROM pipeline_stages WHERE funil = 'remarketing')"
        )
    )
    conn.execute(sa.text("DELETE FROM pipeline_stages WHERE funil = 'remarketing'"))
    op.drop_column("pipeline_stages", "funil")
