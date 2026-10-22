"""add nome_editado_manual (BOOL NOT NULL DEFAULT false) em leads e leads_snapshot

Flag de proteção contra o sync Kommo→local: quando a corretora edita o nome do
lead no painel (ex: "Lead #13344706" → nome real), marcamos nome_editado_manual
= true. O sync (inspector _upsert_snapshot em leads_snapshot; lead_spine shadow
em leads.nome) passa a NÃO sobrescrever o campo `nome` de leads com essa flag —
o resto do sync (telefone, etapa, origem, valor…) segue normal.

Nada é escrito no Kommo. A edição é só no nosso banco. Ambas as tabelas ganham
a flag porque o nome exibido tem duas fontes:
  • leads_snapshot.nome → header da conversa (/painel/conversa) e /painel/kanban
    (via _lead_conversa_contexto)
  • leads.nome          → /painel/crm/board (espinha canônica)

Revision ID: 0049
Revises: 0048
Create Date: 2026-07-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0049"
down_revision: Union[str, None] = "0048"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "leads",
        sa.Column(
            "nome_editado_manual",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "leads_snapshot",
        sa.Column(
            "nome_editado_manual",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("leads_snapshot", "nome_editado_manual")
    op.drop_column("leads", "nome_editado_manual")
