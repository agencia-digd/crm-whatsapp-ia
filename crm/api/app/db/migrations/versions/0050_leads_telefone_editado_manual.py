"""add telefone_editado_manual (BOOL NOT NULL DEFAULT false) em leads e leads_snapshot

Espelha a migration 0049 (nome_editado_manual), mas pro TELEFONE. Quando a
corretora corrige o telefone do lead no painel (ex: lead incontactável com número
inválido "+55000000000009" → número real), marcamos telefone_editado_manual = true.
O sync (inspector _upsert_snapshot em leads_snapshot.telefone; lead_spine shadow
em leads.telefone) passa a NÃO sobrescrever o campo `telefone` de leads com essa
flag — o resto do sync (nome protegido pela 0049, etapa, origem, valor…) segue.

Nada é escrito no Kommo. A edição é só no nosso banco. Ambas as tabelas ganham a
flag porque o telefone tem duas fontes:
  • leads_snapshot.telefone → header/resolução da thread do WhatsApp (por sufixo)
  • leads.telefone          → espinha canônica (/painel/crm/board)

Revision ID: 0050
Revises: 0049
Create Date: 2026-07-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0050"
down_revision: Union[str, None] = "0049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "leads",
        sa.Column(
            "telefone_editado_manual",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "leads_snapshot",
        sa.Column(
            "telefone_editado_manual",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("leads_snapshot", "telefone_editado_manual")
    op.drop_column("leads", "telefone_editado_manual")
