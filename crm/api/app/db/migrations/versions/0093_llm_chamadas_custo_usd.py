"""llm_chamadas.custo_usd — quanto cada chamada de IA custou, em dólar

A tela /gastos do acme-painel já lê esta coluna (SQL_TEM_CUSTO): enquanto ela não
existe, o custo da IA por serviço aparece como "sem registro". Com ela, a tela
passa a somar sozinha.

Preenchida pelo ContabilProvider a partir de app/llm/precos.py. NULL = modelo sem
preço cadastrado (ou chamada anterior a esta migration): nunca vira 0.

Migration 100% ADITIVA: uma coluna nullable, sem default, sem backfill.

Revision ID: 0093
Revises: 0092b
Create Date: 2026-10-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0093"
down_revision: Union[str, None] = "0092b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("llm_chamadas", sa.Column("custo_usd", sa.Numeric(12, 6), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_chamadas", "custo_usd")
