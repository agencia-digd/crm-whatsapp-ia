"""add consumed_by_pessoa column to lead_analyses

Revision ID: 0022
Revises: 0021
Create Date: 2026-05-11

Onda 7 · Fase 3C — destinatário paralelo Pessoa Pessoa9 (WhatsApp).
Marca True só após envio bem-sucedido pro WhatsApp da Pessoa, permitindo
re-envio se primeira tentativa falhar (semântica simétrica a consumed_by_dona).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "lead_analyses",
        sa.Column(
            "consumed_by_pessoa",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("lead_analyses", "consumed_by_pessoa")
