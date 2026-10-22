"""transferência de lead entre corretoras — flags de proteção do sync

Suporte ao POST /painel/crm/transferir (Chat V2): admin transfere qualquer
lead; corretora só transfere lead PRÓPRIO. A transferência muda o dono nas 3
fontes de escopo (leads.owner_id, leads_snapshot.corretora_id e
lead_analyses.corretora_id) e grava lead_events tipo='transferencia'.

Sem proteção, o próximo sync do Kommo desfaria a transferência (o Kommo segue
com o responsible antigo — NUNCA escrevemos de volta lá). Mesmo padrão das
migrations 0049 (nome) e 0050 (telefone):

  • leads.owner_editado_manual          → lead_spine.upsert_lead_shadow NÃO
    sobrescreve owner_id quando true
  • leads_snapshot.corretora_editado_manual → inspector _upsert_snapshot NÃO
    sobrescreve corretora_id quando true

Revision ID: 0061
Revises: 0060
Create Date: 2026-07-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0061"
down_revision: Union[str, None] = "0060"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "leads",
        sa.Column(
            "owner_editado_manual",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "leads_snapshot",
        sa.Column(
            "corretora_editado_manual",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("leads_snapshot", "corretora_editado_manual")
    op.drop_column("leads", "owner_editado_manual")
