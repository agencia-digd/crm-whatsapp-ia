"""create internos_acme + seed Pessoa + Dona (modo teste) — Onda 2

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-09

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "internos_acme",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("nome", sa.Text(), nullable=False),
        sa.Column("telefone_normalizado", sa.Text(), nullable=False, unique=True),
        sa.Column("papel", sa.Text(), nullable=False),
        sa.Column("ativa", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("modo_teste", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("observacao", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "papel IN ('dona')",
            name="ck_internos_acme_papel",
        ),
    )

    bind = op.get_bind()
    bind.execute(
        sa.text(
            "INSERT INTO internos_acme "
            "(nome, telefone_normalizado, papel, modo_teste, observacao) VALUES "
            "(:n1, :t1, 'dona', false, NULL), "
            "(:n2, :t2, 'dona', true, :obs)"
        ),
        {
            "n1": "Pessoa Pessoa9 Acme",
            "t1": "+5500000000000",
            "n2": "Pessoa Dona",
            "t2": "+5500000000000",
            "obs": (
                "modo teste enquanto Evolution só aceita esse número; "
                "remover com: DELETE FROM internos_acme WHERE "
                "telefone_normalizado='+5500000000000';"
            ),
        },
    )


def downgrade() -> None:
    op.drop_table("internos_acme")
