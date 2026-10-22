"""etapas da roda do remarketing: Fila (23), Disparado (24) e Respondeu (25), enfim no repositório

As três etapas existem em produção desde 26/07/2026 e foram criadas POR FORA,
sem migration. `remarketing_alvo.py` fixa os ids (`ETAPA_FILA_PADRAO = 23`,
`ETAPA_DISPARADO = 24`, `ETAPA_RESPONDEU = 25`) e `leads.stage_id` tem FK pra
`pipeline_stages`. Enquanto só existia o banco da Acme isso não doía: as
linhas estavam lá.

O que doía, medido na faxina da suíte em 05/10/2026
(`projetos/acme/relatorios/FAXINA-SUITE-COPILOTO.md`, achado 2): num banco
montado a partir das migrations — ambiente novo, restauração, cliente novo do
CRM — o carrossel morre em `ForeignKeyViolation` no primeiro lead que entra na
fila. Eram 27 testes vermelhos mostrando exatamente isso.

━━ AS LINHAS SÃO AS DE PRODUÇÃO, CAMPO POR CAMPO ━━

Lidas no banco vivo em 05/10/2026, em transação somente-leitura:

    id | tenant_id | nome      | ordem | tipo | funil
    23 |         1 | Fila      |     7 | NULL | remarketing
    24 |         1 | Disparado |     8 | NULL | remarketing
    25 |         1 | Respondeu |     9 | NULL | remarketing

━━ ON CONFLICT DO NOTHING: EM PRODUÇÃO ISTO É NO-OP ━━

Lá as três já existem, com 4.888 leads apontando pra elas. O INSERT não
encosta em linha existente — nem pra "corrigir" nome ou ordem.

━━ ID OCUPADO POR OUTRA COISA FAZ A MIGRATION PARAR ━━

`DO NOTHING` calado seria perigoso num banco em que o 23, 24 ou 25 já fosse
etapa de outro cliente ou de outro funil: o carrossel passaria a tratar essa
etapa como a fila dele. Por isso, depois do INSERT, a migration confere tenant
e funil das três e falha com mensagem em vez de seguir. Nome e ordem não entram
na conferência: são texto e posição de tela.

━━ A SEQUENCE SÓ ANDA PRA FRENTE ━━

Linha com id explícito não move a sequence. Num banco montado pelas migrations
a 0077 já a deixou em 26 ou mais; num banco restaurado só de estrutura ela
nasce em 1, e o id automático bateria no 23. O `setval` aqui usa o MAIOR entre
o maior id e o valor atual da sequence — nunca anda pra trás. Em produção
(maior id 26, sequence em 26) fica onde está.

━━ O DOWNGRADE NÃO APAGA ━━

De propósito, como na 0088. Esta migration não criou as linhas de produção,
então desfazê-la não pode derrubá-las — e apagar etapa com lead dentro quebra
a FK de qualquer jeito.

Revision ID: 0089
Revises: 0088
Create Date: 2026-10-05
"""
from __future__ import annotations

from alembic import op

revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO pipeline_stages (id, tenant_id, nome, funil, ordem)
        VALUES (23, 1, 'Fila',      'remarketing', 7),
               (24, 1, 'Disparado', 'remarketing', 8),
               (25, 1, 'Respondeu', 'remarketing', 9)
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        """
        DO $$
        DECLARE
            estranhas text;
        BEGIN
            SELECT string_agg(format('%s (tenant %s, funil %s, "%s")',
                                     id, tenant_id, funil, nome), '; ' ORDER BY id)
              INTO estranhas
              FROM pipeline_stages
             WHERE id IN (23, 24, 25)
               AND (tenant_id <> 1 OR funil <> 'remarketing');
            IF estranhas IS NOT NULL THEN
                RAISE EXCEPTION
                    'etapas 23/24/25 ocupadas por outra coisa: %. O carrossel fixa esses ids como Fila/Disparado/Respondeu do remarketing do tenant 1.',
                    estranhas;
            END IF;
        END $$
        """
    )
    # Depois da conferência: setval não volta atrás em rollback.
    op.execute(
        """
        SELECT setval('pipeline_stages_id_seq',
                      GREATEST((SELECT max(id) FROM pipeline_stages),
                               (SELECT last_value FROM pipeline_stages_id_seq)))
        """
    )


def downgrade() -> None:
    # No-op deliberado — ver "O DOWNGRADE NÃO APAGA" no cabeçalho.
    pass
