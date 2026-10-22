"""create recovery_conversations table

Revision ID: 0030
Revises: 0029
Create Date: 2026-05-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "recovery_conversations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lead_id", sa.Text(), nullable=False, unique=True, index=True),
        sa.Column("lead_phone", sa.Text(), nullable=False),
        sa.Column("lead_name", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False, server_default="aguardando"),
        sa.Column("turns", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reengage_done", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("end_reason", sa.Text(), nullable=True),
        sa.Column("qualified_data", sa.JSON(), nullable=True),
        sa.Column("hsm_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_lead_msg_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_corretor_msg_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint(
            "state IN ('aguardando','qualificando','reengajado',"
            "'qualificado','perdido','largado')",
            name="ck_recovery_state",
        ),
        sa.CheckConstraint(
            "end_reason IS NULL OR end_reason IN "
            "('ja_comprou','sem_interesse','aluguel','opt_out','pediu_humano')",
            name="ck_recovery_end_reason",
        ),
    )
    op.create_index(
        "ix_recovery_conv_state", "recovery_conversations", ["state"]
    )
    op.create_index(
        "ix_recovery_conv_phone", "recovery_conversations", ["lead_phone"]
    )

    op.create_table(
        "recovery_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lead_id", sa.Text(), nullable=False, index=True),
        sa.Column("role", sa.Text(), nullable=False),  # 'lead' | 'corretor'
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "ts", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint("role IN ('lead','corretor')", name="ck_recovery_msg_role"),
    )
    op.create_index(
        "ix_recovery_msg_lead_ts", "recovery_messages", ["lead_id", "ts"]
    )


def downgrade() -> None:
    op.drop_index("ix_recovery_msg_lead_ts", table_name="recovery_messages")
    op.drop_table("recovery_messages")
    op.drop_index("ix_recovery_conv_phone", table_name="recovery_conversations")
    op.drop_index("ix_recovery_conv_state", table_name="recovery_conversations")
    op.drop_table("recovery_conversations")
