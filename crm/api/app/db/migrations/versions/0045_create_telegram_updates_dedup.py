"""create telegram_updates_processed — dedup/idempotência do webhook Telegram (P1)

FURO 3 (review 2026-07-10): o webhook do Telegram (app/api/telegram_webhook.py)
não deduplicava update_id/message_id nem serializava por chat. Uma reentrega do
Telegram (retry por timeout) ou duas requisições simultâneas rodavam a MESMA
instrução 2x — inclusive tools mutáveis (criar_visita, registrar_venda, enviar).

Esta tabela registra cada update_id JÁ processado. O webhook faz um claim
transacional (INSERT ... ON CONFLICT DO NOTHING) ANTES de executar o agente:
quem inseriu a linha segue; a duplicata (conflito) é descartada sem re-executar.

    telegram_updates_processed(update_id PK, chat_id, message_id, claimed_at)

update_id é único e monotônico por bot (garantia do Telegram) — PK natural.

Revision ID: 0045
Revises: 0044
Create Date: 2026-07-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0045"
down_revision: Union[str, None] = "0044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "telegram_updates_processed",
        sa.Column("update_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    # varredura/limpeza por antiguidade (retenção) usa claimed_at
    op.create_index(
        "ix_telegram_updates_processed_claimed_at",
        "telegram_updates_processed",
        ["claimed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_telegram_updates_processed_claimed_at",
        table_name="telegram_updates_processed",
    )
    op.drop_table("telegram_updates_processed")
