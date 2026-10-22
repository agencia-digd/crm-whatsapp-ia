"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-03

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "corretoras",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("nome", sa.Text(), nullable=False),
        sa.Column("telefone", sa.Text(), nullable=False),
        sa.Column("kommo_user_id", sa.Integer(), nullable=True),
        sa.Column("ativo", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "leads_snapshot",
        sa.Column("lead_id", sa.Text(), nullable=False),
        sa.Column("nome", sa.Text(), nullable=True),
        sa.Column("telefone", sa.Text(), nullable=True),
        sa.Column("corretora_id", sa.Integer(), nullable=True),
        sa.Column("etapa_kommo", sa.Text(), nullable=True),
        sa.Column("origem", sa.Text(), nullable=True),
        sa.Column("criado_em_kommo", sa.TIMESTAMP(), nullable=True),
        sa.Column("ultima_sync", sa.TIMESTAMP(), nullable=True),
        sa.ForeignKeyConstraint(["corretora_id"], ["corretoras.id"]),
        sa.PrimaryKeyConstraint("lead_id"),
    )

    op.create_table(
        "lead_analyses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lead_id", sa.Text(), nullable=False),
        sa.Column("analyzed_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("now()")),
        sa.Column("raw_analysis", JSONB(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("temperatura", sa.Text(), nullable=True),
        sa.Column("has_gap", sa.Boolean(), nullable=True),
        sa.Column("consumed_by_dona", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("consumed_by_corretora", sa.Boolean(), nullable=False, server_default="false"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads_snapshot.lead_id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "briefings_sent",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("destinatario", sa.Text(), nullable=False),
        sa.Column("canal", sa.Text(), nullable=False),
        sa.Column("conteudo", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("now()")),
        sa.Column("success", sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "patterns_weekly",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("corretora_id", sa.Integer(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("pattern_type", sa.Text(), nullable=False),
        sa.Column("occurrences", sa.Integer(), nullable=False),
        sa.Column("detail", JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["corretora_id"], ["corretoras.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("patterns_weekly")
    op.drop_table("briefings_sent")
    op.drop_table("lead_analyses")
    op.drop_table("leads_snapshot")
    op.drop_table("corretoras")
