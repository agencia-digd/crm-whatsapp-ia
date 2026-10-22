"""create inspector_runs table

Revision ID: 0020
Revises: 0019
Create Date: 2026-05-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "inspector_runs",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("corretora_id", sa.Integer(), sa.ForeignKey("corretoras.id"), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("mensagens_processadas", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("leads_analisados", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("erro", sa.Text(), nullable=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_inspector_runs_corretora_finished",
        "inspector_runs",
        ["corretora_id", sa.text("finished_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_inspector_runs_corretora_finished", table_name="inspector_runs")
    op.drop_table("inspector_runs")
