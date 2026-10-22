"""cria historico_cache — cache persistente do /painel/historico

O endpoint /painel/historico recalculava funil + Meta Ads de CADA mês do ano
a cada chamada (~60-100s por request). O dado é quase estático: mês fechado
nunca muda; só o mês corrente evolui ao longo do dia.

Uma linha por mês ('YYYY-MM') com o payload pronto. Validade decidida na
leitura (painel.py):
  • mês fechado calculado após o fechamento → imutável;
  • mês aberto → TTL curto (15 min).
`?force=1` (admin) apaga as linhas do ano e recalcula — cobre registro
retroativo (venda/lead lançado atrasado em mês já fechado).

Revision ID: 0059
Revises: 0058
Create Date: 2026-07-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0059"
down_revision: Union[str, None] = "0058"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "historico_cache",
        sa.Column("ano_mes", sa.String(length=7), primary_key=True),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("historico_cache")
