"""add canal (TEXT NOT NULL DEFAULT 'evolution') em corretoras + Pessoa12='cloud'

O Inspector lê a conversa de cada corretora batendo na instância Evolution via
HTTP (check_instance_status + build_jid_lookup_map). A Pessoa12 (id=3) MIGROU pra Meta
Cloud API oficial: a instância Evolution dela (acme_corretora_a) fica status=close
de propósito, e o webhook cloud_corretora_a grava tudo em mensagens_whatsapp (mesmo
schema). Sem sinalizar o canal, o Inspector via a Pessoa12 como "instancia_offline" e
não analisava nada dela.

A coluna `canal` marca a fonte de verdade da conversa por corretora:
  • 'evolution' (default) → lê pela instância Evolution (Pessoa10, Pessoa1…)
  • 'cloud'              → lê da TABELA mensagens_whatsapp (Pessoa12), pulando o
                           check/leitura da instância Evolution.

Pessoa12 (id=3) nasce 'cloud'; todas as demais ficam no default 'evolution'.

Revision ID: 0056
Revises: 0055
Create Date: 2026-07-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0056"
down_revision: Union[str, None] = "0055"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "corretoras",
        sa.Column(
            "canal",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'evolution'"),
        ),
    )
    # Pessoa12 (id=3) já opera 100% na Cloud API (coexistência 2591). Marca o canal.
    op.execute("UPDATE corretoras SET canal = 'cloud' WHERE id = 3")


def downgrade() -> None:
    op.drop_column("corretoras", "canal")
