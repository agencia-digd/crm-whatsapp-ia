"""shadow_cadencia ganha ciclo de vida: sombra → fila de aprovação → uso

A sombra (0065) provou que a IA acha contexto real em ~metade dos follow-ups.
A Dona aprovou usar de verdade, mas com REVISÃO HUMANA antes de sair: ninguém
manda pro cliente da Acme um texto que nenhuma pessoa leu.

Este lote transforma o registro de sombra num item de FILA:

    sombra     → gerada só pra comparar, nunca sai (modo experimento)
    pendente   → esperando alguém aprovar no painel
    aprovada   → liberada; a cadência usa `texto_final` no lugar do template
    rejeitada  → descartada; a cadência usa o template de sempre
    usada      → já saiu (guarda quando, pra auditoria)
    expirada   → o toque passou sem aprovação; virou template

`texto_final` existe separado de `sugestao_ia` de propósito: quem aprova pode
EDITAR. Assim a gente sabe o que a IA escreveu E o que de fato foi enviado —
sem isso não dá pra medir se a IA está melhorando ou se é a humana consertando.

Migration ADITIVA: só ALTER ADD COLUMN + índice. Nenhuma coluna removida,
nenhum dado reescrito além do default de `status`.

Revision ID: 0066
Revises: 0065
Create Date: 2026-07-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0066"
down_revision: Union[str, None] = "0065"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # As 28 linhas que já existem são do experimento → nascem como 'sombra'.
    op.add_column(
        "shadow_cadencia",
        sa.Column("status", sa.Text(), nullable=False, server_default="sombra"),
    )
    # o que de fato vai/foi pro lead (pode ser a sugestão editada por quem aprovou)
    op.add_column("shadow_cadencia", sa.Column("texto_final", sa.Text(), nullable=True))
    op.add_column("shadow_cadencia", sa.Column("aprovado_por", sa.Text(), nullable=True))
    op.add_column(
        "shadow_cadencia",
        sa.Column("aprovado_em", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "shadow_cadencia",
        sa.Column("usada_em", sa.DateTime(timezone=True), nullable=True),
    )
    # a fila é lida por status (pendente primeiro) e a cadência busca por
    # (telefone, toque) na hora de disparar — daí os dois índices.
    op.create_index(
        "ix_shadow_cadencia_status", "shadow_cadencia", ["status", "created_at"]
    )
    op.create_index(
        "ix_shadow_cadencia_lookup", "shadow_cadencia", ["lead_telefone", "toque", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_shadow_cadencia_lookup", table_name="shadow_cadencia")
    op.drop_index("ix_shadow_cadencia_status", table_name="shadow_cadencia")
    op.drop_column("shadow_cadencia", "usada_em")
    op.drop_column("shadow_cadencia", "aprovado_em")
    op.drop_column("shadow_cadencia", "aprovado_por")
    op.drop_column("shadow_cadencia", "texto_final")
    op.drop_column("shadow_cadencia", "status")
