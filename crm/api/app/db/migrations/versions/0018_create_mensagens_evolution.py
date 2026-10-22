"""create mensagens_whatsapp — log persistente de mensagens WhatsApp

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mensagens_whatsapp",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("evolution_message_id", sa.Text(), nullable=False),
        sa.Column("instance", sa.Text(), nullable=False),
        sa.Column(
            "corretora_id",
            sa.Integer(),
            sa.ForeignKey("corretoras.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("remote_jid", sa.Text(), nullable=False),
        sa.Column("contato_telefone", sa.Text(), nullable=True),
        sa.Column("contato_nome", sa.Text(), nullable=True),
        sa.Column("from_corretora", sa.Boolean(), nullable=False),
        sa.Column("tipo_mensagem", sa.Text(), nullable=False),
        sa.Column("conteudo_texto", sa.Text(), nullable=True),
        sa.Column("conteudo_metadata", JSONB(), nullable=True),
        sa.Column("timestamp_evolution", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recebido_em",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("raw_payload", JSONB(), nullable=True),
        sa.UniqueConstraint("evolution_message_id", "instance", name="uq_mensagem_instance"),
    )

    op.create_index(
        "idx_mensagens_evolution_corretora_ts",
        "mensagens_whatsapp",
        ["corretora_id", sa.text("timestamp_evolution DESC")],
    )
    op.create_index(
        "idx_mensagens_evolution_telefone_ts",
        "mensagens_whatsapp",
        ["contato_telefone", sa.text("timestamp_evolution DESC")],
    )


def downgrade() -> None:
    op.drop_index("idx_mensagens_evolution_telefone_ts", table_name="mensagens_whatsapp")
    op.drop_index("idx_mensagens_evolution_corretora_ts", table_name="mensagens_whatsapp")
    op.drop_table("mensagens_whatsapp")
