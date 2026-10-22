"""visita_lembrete_d1: o lembrete de véspera que a corretora recebe, e o que ela respondeu

Setembro (01-19/2026): 104 leads → 21 visitas marcadas → 10 CANCELADAS (47,6%)
→ 8 realizadas → 3 vendas. O gargalo da Acme não é verba de mídia, é visita
que não acontece. Recuperar metade das canceladas vale ~1 venda por mês com
R$ 0 de mídia.

O `confirmar_domingo.py` é retrovisor: pergunta no domingo se a visita da semana
passada aconteceu. Esta tabela é o espelho da frente — o lembrete da VÉSPERA,
pra corretora, com a resposta dela de volta no sistema.

━━ POR QUE UMA TABELA, E NÃO UMA COLUNA EM `visitas` ━━

Porque o que interessa medir não é o estado final da visita, é o CICLO DA
COBRANÇA: saiu o lembrete? por qual canal? a corretora respondeu? em quanto
tempo? ficou sem resposta na hora do corte? Isso é um evento com vida própria,
e enfiar cinco colunas em `visitas` mistura o fato (a visita) com o processo
(a cobrança). Também deixa a tabela `visitas` fora do caminho — ela é lida por
meia dúzia de queries do painel que ninguém quer reabrir por causa disto.

━━ `ordem_no_lote` ━━

A corretora recebe as visitas dela NUMERADAS e responde "1 ok, 2 remarcou". O
número é o contrato entre a mensagem e o parser: sem ele, casar "remarcou" com
a visita certa viraria adivinhação de nome de cliente no meio de um texto de
WhatsApp. Ele é atribuído no momento do envio e nunca muda.

━━ O UNIQUE ━━

Um lembrete por visita, para sempre (`uq_lembrete_d1_visita`). Se o tick rodar
de novo às 18h10, a mesma visita não vira segunda mensagem — o INSERT bate no
índice e o envio nem começa. Mandar a véspera duas vezes é o jeito mais rápido
de a corretora parar de ler a véspera.

━━ POR QUE 0086, SE NASCEU 0082 ━━

Foi escrita em 19/09 como 0082/0081 e ficou parada numa branch. Enquanto isso a
main ganhou outra 0082 (`corretoras_nome_publico`) e andou até a 0085. Com dois
arquivos dizendo `revision = "0082"` o Alembic enxerga duas cabeças e recusa
qualquer `upgrade head` — o do lembrete e o de todo mundo. Renumerada em
30/09/2026 pro fim da fila, antes de entrar na main. O conteúdo não mudou.

Revision ID: 0086
Revises: 0085
Create Date: 2026-09-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "visita_lembrete_d1",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("visita_id", sa.BigInteger, nullable=False),
        sa.Column("corretora_id", sa.Integer, nullable=True),
        # Dia da visita em SP (não do envio): é a chave de agrupamento do lote e
        # o que a hora de corte da manhã seguinte consulta.
        sa.Column("data_visita", sa.Date, nullable=False),
        # 1..N dentro do lote da corretora naquele dia — o número que ela responde.
        sa.Column("ordem_no_lote", sa.Integer, nullable=False),
        sa.Column("telefone_corretora", sa.Text, nullable=False),
        sa.Column("lead_nome", sa.Text, nullable=True),
        # 'texto_livre' (janela de 24h aberta) ou 'template' (aviso + recado que
        # o clique em "Saiba mais" entrega). NULL enquanto não saiu.
        sa.Column("canal", sa.Text, nullable=True),
        sa.Column("enviado_em", sa.DateTime(timezone=True), nullable=True),
        # NULL = ainda sem resposta. 'nao_entendido' é resposta que chegou e o
        # parser não decifrou — NUNCA vira mudança de status por palpite.
        sa.Column("desfecho", sa.Text, nullable=True),
        sa.Column("desfecho_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resposta_texto", sa.Text, nullable=True),
        # Carimbo da hora de corte: a corretora não respondeu até a manhã do dia
        # da visita. É o que o painel mostra e o que o aviso pra Dona conta.
        sa.Column("sem_resposta_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(
            "desfecho IS NULL OR desfecho IN "
            "('confirmada','remarcada','cancelada','nao_entendido')",
            name="ck_lembrete_d1_desfecho",
        ),
        sa.UniqueConstraint("tenant_id", "visita_id", name="uq_lembrete_d1_visita"),
    )
    op.create_index("ix_lembrete_d1_data", "visita_lembrete_d1", ["data_visita"])
    op.create_index("ix_lembrete_d1_desfecho", "visita_lembrete_d1", ["desfecho"])
    # A leitura de respostas varre "enviado e ainda sem desfecho", por corretora.
    op.create_index(
        "ix_lembrete_d1_pendentes", "visita_lembrete_d1",
        ["corretora_id", "data_visita"],
    )


def downgrade() -> None:
    op.drop_index("ix_lembrete_d1_pendentes", table_name="visita_lembrete_d1")
    op.drop_index("ix_lembrete_d1_desfecho", table_name="visita_lembrete_d1")
    op.drop_index("ix_lembrete_d1_data", table_name="visita_lembrete_d1")
    op.drop_table("visita_lembrete_d1")
