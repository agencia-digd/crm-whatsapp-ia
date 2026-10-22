"""create conversations table (Onda 2)

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-09

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("sender_kind", sa.Text(), nullable=False),
        sa.Column("sender_id", sa.BigInteger(), nullable=True),
        sa.Column("message_role", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "sender_kind IN ('corretora','dona','assistant')",
            name="ck_conversations_sender_kind",
        ),
        sa.CheckConstraint(
            "message_role IN ('user','assistant')",
            name="ck_conversations_message_role",
        ),
    )
    op.create_index(
        "idx_conversations_phone_time",
        "conversations",
        ["phone", sa.text("created_at DESC")],
    )
    op.execute(
        "CREATE UNIQUE INDEX idx_conversations_msgid "
        "ON conversations(message_id) WHERE message_id IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index("idx_conversations_msgid", table_name="conversations")
    op.drop_index("idx_conversations_phone_time", table_name="conversations")
    op.drop_table("conversations")
