"""leads.telefone_invalido — triagem de telefone ambíguo (L3 · identidade de lead)

Contexto (auditoria 26/07): a base tem 48 leads com 12 dígitos contra 6.404 com
13. Desses 48, 34 são celular que perdeu o 9 (assinante começa com '9', caso
inequívoco), 5 são fixo legítimo (começa com '3' — inclui a faixa 3211 da
própria linha 0000 da Acme) e 9 são AMBÍGUOS (começam com 7/8: pode ser fixo
antigo ou celular sem o 9).

`canonical_phone_info` não decide os ambíguos. Esta coluna marca o lead pra
triagem humana em vez de (a) auto-reparar e corromper o número, ou (b) jogar o
lead pro limbo — o intake está no caminho vivo da Meta, perder lead é pior.

Só cria a coluna. A correção em lote dos 34 celulares e o merge dos 8 pares
duplicados são SQL de dados, propostos no relatório do L3 e executados pela
Dona — não entram em migration.

Revision ID: 0063
Revises: 0062
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0063"
down_revision: Union[str, None] = "0062"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "leads",
        sa.Column(
            "telefone_invalido",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Índice parcial: a triagem só lê as linhas marcadas (hoje ~9 de 6.4k).
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_leads_telefone_invalido "
        "ON leads (tenant_id) WHERE telefone_invalido"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_leads_telefone_invalido")
    op.drop_column("leads", "telefone_invalido")
