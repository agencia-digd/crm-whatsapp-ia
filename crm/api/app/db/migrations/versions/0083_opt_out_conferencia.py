"""opt_out_conferencia: o que a segunda opinião tem a dizer, pra um humano olhar

A regra (`app/services/opt_out.py`) decide o opt-out e continua sendo a única
que grava. Esta tabela é o lado de fora dessa decisão: onde o classificador
(corretor · `app/services/opt_out_corretor.py`) discorda dela ou vê algo que ela não viu.

Medido em 21/09/2026 sobre 42.521 mensagens de 14 meses
(`projetos/acme/RELATORIO-CORRETOR-OPTOUT.md`):

    hoje                         35 acertos / 7 erros
    com veto (0,50) + rede (0,85)  40 acertos / 2 erros

O preço dessa diferença são 7 linhas nesta tabela em 14 meses — 0,06 por dia.
Não é fila de trabalho, é uma linha a cada duas semanas.

━━ POR QUE ELA NÃO MUDA NADA ━━

Nenhuma linha daqui descadastra, reativa ou move card. `veto` NÃO apaga o
opt-out que a regra gravou — pede conferência e o opt-out segue valendo;
`rede` NÃO chama `registrar()` — pede conferência e ninguém foi descadastrado.
O motivo está medido: a nota do classificador é propriedade do ENUNCIADO, não
do caso. Reescrever a pergunta moveu a nota de uma mesma fala de 0,10 pra 0,82
(+0,72), enquanto repetir a mesma pergunta move 0,01. Corte calibrado hoje não
sobrevive a mexer no texto da pergunta — então ele sugere, e a humana decide.

━━ POR QUE `texto` E `enunciado` FICAM NA LINHA ━━

Quem confere precisa da fala pra julgar, e precisa saber com que pergunta
aquela nota saiu. Linha antiga sem o enunciado dela é número sem unidade.

━━ IDEMPOTENTE ━━

`CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`: rodar a migration
duas vezes (ou num banco onde a tabela já veio por `create_all` na suíte) não
levanta. O UNIQUE é a trava de duplicata: uma conferência por (mensagem, tipo),
pra varredura que rodar de novo não gerar a mesma linha duas vezes.

Revision ID: 0083
Revises: 0082
Create Date: 2026-09-21
"""
from __future__ import annotations

from alembic import op

revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS opt_out_conferencia (
            id            BIGSERIAL PRIMARY KEY,
            tenant_id     INTEGER     NOT NULL DEFAULT 1,
            tipo          TEXT        NOT NULL,
            telefone      TEXT,
            mensagem_id   UUID,
            texto         TEXT,
            nota          NUMERIC(4,3),
            regra_marcou  BOOLEAN     NOT NULL,
            modelo        TEXT,
            enunciado     TEXT,
            criado_em     TIMESTAMPTZ NOT NULL DEFAULT now(),
            conferido_em  TIMESTAMPTZ,
            conferido_por TEXT,
            veredito      TEXT
        )
        """
    )
    # Fila: o que ninguém olhou ainda, mais novo primeiro.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_opt_out_conferencia_pendente "
        "ON opt_out_conferencia (conferido_em, criado_em)"
    )
    # Uma linha por (mensagem, tipo). Parcial porque mensagem_id pode ser nula
    # (conferência de fala avulsa, fora da tabela de mensagens) e NULL não
    # colide com NULL em UNIQUE — sem o WHERE o índice não travaria nada ali.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_opt_out_conferencia_msg "
        "ON opt_out_conferencia (mensagem_id, tipo) WHERE mensagem_id IS NOT NULL"
    )


def downgrade() -> None:
    # Só linhas de aviso: nada depende delas, ninguém foi marcado por causa
    # delas. Cai inteira.
    op.execute("DROP TABLE IF EXISTS opt_out_conferencia")
