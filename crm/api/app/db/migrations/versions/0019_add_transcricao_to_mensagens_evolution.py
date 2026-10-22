"""add transcricao fields to mensagens_whatsapp

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("mensagens_whatsapp", sa.Column("transcricao", sa.Text(), nullable=True))
    op.add_column("mensagens_whatsapp", sa.Column("transcricao_modelo", sa.String(50), nullable=True))
    op.add_column("mensagens_whatsapp", sa.Column("transcricao_em", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "mensagens_whatsapp",
        sa.Column(
            "transcricao_status",
            sa.String(20),
            nullable=False,
            server_default="pendente",
        ),
    )
    op.add_column("mensagens_whatsapp", sa.Column("transcricao_erro", sa.Text(), nullable=True))

    op.create_index(
        "ix_mensagens_evolution_transcricao_status_pendente",
        "mensagens_whatsapp",
        ["transcricao_status"],
        postgresql_where=sa.text("transcricao_status = 'pendente'"),
    )

    # Backfill: áudios → 'pendente', resto → 'nao_aplicavel'
    op.execute(
        """
        UPDATE mensagens_whatsapp
        SET transcricao_status = 'nao_aplicavel'
        WHERE tipo_mensagem != 'audio'
        """
    )
    # áudios já estão com server_default 'pendente' — nada a fazer para eles


def downgrade() -> None:
    op.drop_index(
        "ix_mensagens_evolution_transcricao_status_pendente",
        table_name="mensagens_whatsapp",
    )
    op.drop_column("mensagens_whatsapp", "transcricao_erro")
    op.drop_column("mensagens_whatsapp", "transcricao_status")
    op.drop_column("mensagens_whatsapp", "transcricao_em")
    op.drop_column("mensagens_whatsapp", "transcricao_modelo")
    op.drop_column("mensagens_whatsapp", "transcricao")
