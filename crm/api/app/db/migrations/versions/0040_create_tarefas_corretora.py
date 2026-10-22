"""create tarefas_corretora — tarefa da corretora num lead, aparece no Hoje no dia

Revision ID: 0040
Revises: 0039
Create Date: 2026-07-08
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0040"
down_revision: Union[str, None] = "0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tarefas_corretora",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("lead_id", sa.Text(), nullable=False),
        sa.Column("lead_nome", sa.Text(), nullable=True),
        sa.Column(
            "corretora_id",
            sa.Integer(),
            sa.ForeignKey("corretoras.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("data_tarefa", sa.Date(), nullable=False),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'aberta'")),
        sa.Column("autor", sa.Text(), nullable=True),
        sa.Column("criado_em", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("concluido_em", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('aberta','concluida')",
            name="ck_tarefas_corretora_status",
        ),
    )
    op.create_index(
        "ix_tarefas_corretora_cid_status_data",
        "tarefas_corretora",
        ["corretora_id", "status", "data_tarefa"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tarefas_corretora_cid_status_data", table_name="tarefas_corretora"
    )
    op.drop_table("tarefas_corretora")
