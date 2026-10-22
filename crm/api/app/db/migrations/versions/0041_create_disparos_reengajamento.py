"""create disparos_reengajamento — registro/anti-ban do disparo de reengajamento Pessoa12

Revision ID: 0041
Revises: 0040
Create Date: 2026-07-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "disparos_reengajamento",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("lead_id", sa.Text(), nullable=True),
        sa.Column("corretora_id", sa.Integer(), nullable=True),
        sa.Column("telefone", sa.Text(), nullable=False),
        sa.Column("template", sa.Text(), nullable=True),
        sa.Column("variacao", sa.SmallInteger(), nullable=True),
        sa.Column("texto_enviado", sa.Text(), nullable=True),
        sa.Column("enviado_por", sa.Text(), nullable=True),
        sa.Column(
            "enviado_em",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("respondeu_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "opt_out",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("opt_out_em", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_disparos_reengajamento_cid_tel_enviado",
        "disparos_reengajamento",
        ["corretora_id", "telefone", "enviado_em"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_disparos_reengajamento_cid_tel_enviado",
        table_name="disparos_reengajamento",
    )
    op.drop_table("disparos_reengajamento")
