"""push_inscricoes.usuario: admin também recebe aviso de lead

O push nasceu por corretora — cada uma recebe o lead dela. Mas a Dona entra
como admin (`corretora_id` NULL) e não conseguia nem se inscrever, o que
impedia até de testar no próprio celular.

Inscrição com `corretora_id` NULL passa a significar "admin: recebe TODO lead
novo, de qualquer corretora". `usuario` guarda o login pra saber de quem é o
aparelho — sem isso, duas admins inscritas viram duas linhas indistinguíveis.

Revision ID: 0072
Revises: 0071
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "push_inscricoes",
        sa.Column("usuario", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("push_inscricoes", "usuario")
