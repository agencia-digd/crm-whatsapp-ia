"""disparos_reengajamento.wamid: o elo que faltava entre "mandei" e "chegou"

O copiloto já recebe e grava o status de entrega da Meta: o webhook de cada
linha escreve `mensagens_whatsapp.conteudo_metadata.delivery_status`, casando
por `evolution_message_id = wamid`. Só que o registro do reengajamento não
guardava o wamid — então o status existia no banco e o placar do disparo não
tinha como chegar nele. Contava "mandado" e parava aí.

O custo disso foi medido em 04/10/2026 na Fulano: 909 mensagens, 368 falhas
(40,5%), e o relatório dizendo que tinha saído tudo. Aceite da Meta não é
entrega.

━━ NULLABLE, E SEM BACKFILL ━━

Linha antiga fica NULL. É honesto: o wamid daqueles envios não foi guardado em
lugar nenhum que dê pra casar com certeza (casar por telefone + horário é
chute, e chute em placar de entrega é o problema que esta coluna resolve). O
placar (`app/services/remarketing_placar.py`) conta essas linhas num balde
próprio, "sem rastro" — nem entregue, nem falha.

━━ O ÍNDICE ━━

Parcial (só onde há wamid): a tabela tem anos de linha NULL que nunca vão ser
procuradas por esta chave.

IF NOT EXISTS em tudo: rodar duas vezes é no-op.

Revision ID: 0087
Revises: 0086
Create Date: 2026-10-04
"""
from __future__ import annotations

from alembic import op

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE disparos_reengajamento ADD COLUMN IF NOT EXISTS wamid text")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_disparos_reengajamento_wamid "
        "ON disparos_reengajamento (wamid) WHERE wamid IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_disparos_reengajamento_wamid")
    op.execute("ALTER TABLE disparos_reengajamento DROP COLUMN IF EXISTS wamid")
