"""corretoras.nome_publico: o nome que o CLIENTE lê, separado do nome interno

Incidente 18/09/2026: a Kauane recebeu a primeira mensagem pela linha 2591
dizendo "Aqui é a Pessoa3, da Acme Imóveis". Pro cliente, essa linha é a
**Pessoa** — é o nome que a Pessoa12 usa no atendimento e o proprio verified_name do
numero na Meta e "Pessoa Pessoa9 Acme". O sistema falou um nome que a cliente
nunca vai ouvir de novo.

A causa e que so existia UM nome por corretora (`corretoras.nome`), usado pra
duas coisas diferentes:
  • identidade INTERNA (painel, ranking, briefing, Kommo) → "Pessoa3";
  • identidade PUBLICA, o que vai no texto pro lead → "Pessoa".
Os dois call sites que escrevem pro cliente (`handoff_remarketing.texto_aviso`
e `pos_ligacao._texto`) faziam `nome.split()[0]` e caiam no nome interno.

Por que COLUNA e nao mapa no codigo:
  • ja existiam DOIS mapas parciais espalhados (`briefing_corretora.
    CORRETORA_INFO` com label_remarketing="Pessoa", e os alias Pessoa→Pessoa3
    das skills da Assistente). Mapa numero tres nao resolve, so multiplica a
    chance de divergir;
  • quem sabe o nome que a corretora usa com cliente e a operacao, nao o
    deploy: com coluna, a Dona corrige um UPDATE sem esperar release;
  • corretora nova (Pessoa8 09/09, e as proximas) passa a ter onde declarar
    isso no cadastro, em vez de nascer com o bug silencioso.

NULL = "usa o primeiro nome de `nome`", que e o comportamento historico. So a
Pessoa3 nasce com valor, porque so nela interno != publico.

Revision ID: 0082
Revises: 0081
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "corretoras",
        sa.Column("nome_publico", sa.Text(), nullable=True),
    )
    # A unica divergencia conhecida hoje. Casa com `briefing_corretora.
    # CORRETORA_INFO["Pessoa3"]["label_remarketing"] = "Pessoa"`, que ja dizia
    # a mesma coisa em outro canto do codigo.
    op.execute(
        "UPDATE corretoras SET nome_publico = 'Pessoa' "
        "WHERE evolution_instance = 'acme_corretora_a' AND nome_publico IS NULL"
    )


def downgrade() -> None:
    op.drop_column("corretoras", "nome_publico")
