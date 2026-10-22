"""recado_corretora: um recado avulso que sai quando a corretora abre a janela

O push de briefing (seg/sex) já resolve o ritual: template UTILITY avisa, a
corretora toca em "Saiba mais", a janela de 24h abre e o briefing completo sai
de graça. Mas o gancho do botão só sabia entregar UMA coisa — o briefing
gerado do dia.

Quando a Dona pede "avisa a Pessoa10 que ficou gente sem resposta hoje", isso não
é o briefing: é um recado pontual, escrito na hora, sobre um fato do dia. Sem
lugar pra guardar, o único caminho seria mandar texto livre fora da janela —
que a Meta cobra e, pior, só entrega se a janela já estiver aberta.

Aqui fica o recado pendente. O handler do botão passa a olhar isto primeiro:
tem recado → manda o recado; não tem → briefing de sempre. Um recado por vez
por corretora, consumido no envio (`enviado_em`), com autor registrado — quem
mandou é parte do recado, não metadado.

Revision ID: 0076
Revises: 0075
"""
from __future__ import annotations

from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS recado_corretora (
            id            SERIAL PRIMARY KEY,
            tenant_id     INTEGER NOT NULL DEFAULT 1,
            corretora_id  INTEGER NOT NULL REFERENCES corretoras(id),
            texto         TEXT    NOT NULL,
            criado_por    TEXT    NOT NULL,
            criado_em     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            enviado_em    TIMESTAMPTZ,
            cancelado_em  TIMESTAMPTZ
        )
        """
    )
    # Só UM recado pendente por corretora: se a Dona pedir dois no mesmo dia, o
    # segundo substitui (o service cancela o anterior). Sem isso a corretora
    # tocaria o botão e receberia um recado velho de três dias atrás.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ix_recado_corretora_pendente
            ON recado_corretora (tenant_id, corretora_id)
         WHERE enviado_em IS NULL AND cancelado_em IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_recado_corretora_pendente")
    op.execute("DROP TABLE IF EXISTS recado_corretora")
