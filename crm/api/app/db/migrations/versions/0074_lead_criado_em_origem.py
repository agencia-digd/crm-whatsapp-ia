"""leads.criado_em_origem: quando o cliente entrou DE VERDADE

`leads.created_at` é quando a linha nasceu NA NOSSA base — pra os 555 leads de
nutrição importados em lote, é o mesmo timestamp pra todo mundo (22/07/2026
17:33), a hora da importação. Mostrar isso como "entrou em" seria inventar uma
data: o Douglas entrou no Kommo em 14/08/2025, a Fran em 28/05/2025.

`leads_snapshot.criado_em_kommo` tem a data certa, mas só existe pra 69 dos 555
— snapshot só é criado pra lead que passou pelo sync do Kommo.

Esta coluna guarda a data do SISTEMA DE ORIGEM (hoje só Kommo). Fica em `leads`
porque é atributo do lead, não do cache, e sobrevive a snapshot que não existe.
NULL = não sabemos; a tela mostra "—" em vez de chutar.

Revision ID: 0074
Revises: 0073
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "leads",
        sa.Column("criado_em_origem", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("leads", "criado_em_origem")
