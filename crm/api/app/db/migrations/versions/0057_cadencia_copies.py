"""cadencia_copies — override de copy dos toques da cadência (tela /automacoes)

As copies DEFAULT dos toques seguem no código (`app/services/cadencia.py`:
`_TEMPLATES_MSG` e `_TEMPLATE_REMARKETING`). Esta tabela guarda SÓ o texto
personalizado pela tela de Automações do painel V2:

  - `toque` = índice do toque de mensagem (0, 2, 3, 5, 7);
  - `toque = -1` = mensagem de remarketing (1 msg / 30 dias);
  - sem linha = motor usa o default; DELETE da linha = "restaurar padrão".

O motor (`cadencia.tick`) lê o override na hora de enfileirar (best-effort:
tabela ausente degrada pro default — nada quebra em árvore sem a migration).

Migration 100% ADITIVA: nenhum ALTER em tabela existente.


Revision ID: 0057
Revises: 0050
Create Date: 2026-07-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0057"
down_revision: Union[str, None] = "0056"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cadencia_copies",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False, server_default=sa.text("1")),
        # índice do toque de mensagem (0, 2, 3, 5, 7) · -1 = remarketing
        sa.Column("toque", sa.Integer(), nullable=False),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("atualizado_por", sa.Text(), nullable=True),
        sa.Column(
            "atualizado_em",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("tenant_id", "toque", name="uq_cadencia_copies_tenant_toque"),
    )


def downgrade() -> None:
    op.drop_table("cadencia_copies")
