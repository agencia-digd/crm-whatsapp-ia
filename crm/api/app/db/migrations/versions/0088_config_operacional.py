"""config_operacional: a tabela que a tela de automações grava, enfim no repositório

A tabela existe em produção desde que o interruptor das automações virou botão
de painel (`app/services/config_operacional.py`) — e foi criada POR FORA, sem
migration. Enquanto só existia a Acme isso não doía: a tabela estava lá.

O que doía, medido em 05/10/2026 (`projetos/crm/VERIFICACAO-DISPARO-POR-ETAPA.md`):

  - num banco novo os 9 testes que provam que a tela GRAVA quebravam por
    "relation does not exist", e só passavam com alguém criando a tabela à mão;
  - um segundo cliente nasceria sem ela — e sem ela o painel não desliga nada.

━━ O DDL É O DE PRODUÇÃO, COLUNA POR COLUNA ━━

Conferido no `information_schema` do banco vivo em 05/10: `chave text PK`,
`valor jsonb NOT NULL`, `atualizado_em timestamptz NOT NULL DEFAULT now()`,
`atualizado_por text`. Sem `tenant_id`: a chave é global da instalação.

━━ IF NOT EXISTS: EM PRODUÇÃO ISTO É NO-OP ━━

Lá a tabela já existe, com 12 linhas que SÃO o estado das automações (inclusive
`carrossel_corretor_ativo = false`). `CREATE TABLE IF NOT EXISTS` não encosta nelas.

━━ O DOWNGRADE NÃO APAGA ━━

De propósito. Esta migration não criou a tabela de produção, então desfazê-la
não pode derrubá-la: um `DROP` aqui apagaria todos os interruptores e cada
automação voltaria pro padrão do `.env` — que é como se liga disparo sem querer.

Revision ID: 0088
Revises: 0087
Create Date: 2026-10-05
"""
from __future__ import annotations

from alembic import op

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS config_operacional (
            chave          text PRIMARY KEY,
            valor          jsonb NOT NULL,
            atualizado_em  timestamptz NOT NULL DEFAULT now(),
            atualizado_por text
        )
        """
    )


def downgrade() -> None:
    # No-op deliberado — ver "O DOWNGRADE NÃO APAGA" no cabeçalho.
    pass
