"""create hunter_prospects + hunter_messages tables

Revision ID: 0031
Revises: 0030
Create Date: 2026-05-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "hunter_prospects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("phone", sa.Text(), nullable=False, unique=True, index=True),
        sa.Column("nome", sa.Text(), nullable=True),
        sa.Column("segmento", sa.Text(), nullable=True),
        sa.Column("cidade", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False, server_default="site-hunter"),
        sa.Column("slug", sa.Text(), nullable=True, index=True),
        sa.Column("preview_url", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("turns", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("end_reason", sa.Text(), nullable=True),
        sa.Column("qualified_data", JSONB(), nullable=True),
        sa.Column("hsm_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_lead_msg_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_assistente_msg_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "hunter_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("phone", sa.Text(), nullable=False, index=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=True, unique=True),
        sa.Column("ts", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("hunter_messages")
    op.drop_table("hunter_prospects")
