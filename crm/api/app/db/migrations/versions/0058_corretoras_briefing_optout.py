"""add briefing_optout (BOOLEAN NOT NULL DEFAULT false) em corretoras

Push do briefing 2x/semana (seg+sex) pela linha Cloud da Assistente (2955) via
template UTILITY com 2 botões Quick Reply. O botão "Parar mensagens" é o
opt-out DESTE ritual — não desativa a corretora (ela segue no Inspector,
painel, cadência etc.), só sai da lista do push de briefing.

O opt-out do api-disparos NÃO cobre a linha da Assistente (webhooks.py retorna cedo
pra ASSISTENTE_PHONE_ID antes do fluxo de opt-out), então o estado vive aqui.

  • false (default) → recebe o aviso de briefing seg/sex
  • true            → fora do push (pull "me manda o briefing" continua valendo)

Revision ID: 0058
Revises: 0057
Create Date: 2026-07-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0058"
down_revision: Union[str, None] = "0057"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "corretoras",
        sa.Column(
            "briefing_optout",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("corretoras", "briefing_optout")
