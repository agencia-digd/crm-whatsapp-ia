"""leads.veio_remarketing_em: a tag "remarketing" no card da corretora

Pedido da Dona (15/09/2026), no redesenho do fluxo da Corretor: quando um lead do
remarketing é transferido pra corretora, ele tem que CHEGAR marcado — a Pessoa12
precisa saber de onde a pessoa veio sem abrir o histórico do lead.

Hoje o dado até existe, mas só como `lead_events tipo='transferencia'` com
`payload->>'origem' = 'handoff_remarketing'` — histórico, não tag: ninguém lê
isso no card, e a promoção da Pessoa (`promover_para_vendas`) nem grava esse
evento (grava `promovido_vendas`). Duas fontes diferentes pra mesma pergunta,
nenhuma na tela.

Por que COLUNA DATADA e não string de tag / tabela de tags:
  • é exatamente a convenção que já existe em `leads.adotado_ligacao_em` — a
    outra marca de procedência do lead. Uma casa, um padrão;
  • `leads.origem` não serve: o shadow do Kommo (`lead_spine`) sobrescreve a
    cada sync — a tag sumiria sozinha;
  • tabela de tags genérica seria estrutura nova pra UMA marca. Quando houver
    a segunda, ela é que paga esse custo;
  • datado responde duas perguntas com um campo: "veio do remarketing?"
    (IS NOT NULL) e "quando chegou?".

Contexto: no mesmo dia foram limpos 113 leads presos na etapa "Respondeu" —
metade tinha levantado a mão e ninguém foi atrás. A tag é parte de fazer o
lead do remarketing ser VISÍVEL como tal quando chega na corretora.

Backfill: marca quem JÁ foi transferido pelo handoff automático (evento
`transferencia` com origem `handoff_remarketing`) usando a data do evento —
senão os leads que a Pessoa12 já tem na mão nasceriam sem a tag e a tela mentiria
por omissão. Não inventa data pra quem veio pela promoção da Pessoa: lá não há
evento que prove a procedência.

Revision ID: 0081
Revises: 0080
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "leads",
        sa.Column("veio_remarketing_em", sa.DateTime(timezone=True), nullable=True),
    )
    # Índice parcial: a pergunta do board é sempre "quais TÊM a marca" — a
    # esmagadora maioria é NULL e não precisa entrar no índice.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_leads_veio_remarketing "
        "ON leads (tenant_id, veio_remarketing_em) "
        "WHERE veio_remarketing_em IS NOT NULL"
    )
    op.execute(
        """
        UPDATE leads l
           SET veio_remarketing_em = sub.at
          FROM (
                SELECT lead_id, min(at) AS at
                  FROM lead_events
                 WHERE tipo = 'transferencia'
                   AND payload->>'origem' = 'handoff_remarketing'
                 GROUP BY lead_id
               ) sub
         WHERE sub.lead_id = l.id
           AND l.veio_remarketing_em IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_leads_veio_remarketing")
    op.drop_column("leads", "veio_remarketing_em")
