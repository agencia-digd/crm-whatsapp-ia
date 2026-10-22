"""llm_chamadas — o custo do copiloto para de sumir quando ele troca de provider

09/09/2026: em 06/09 às 00:53 o copiloto inteiro saiu do Claude e foi pro
Alibaba (LLM_PROVIDER=modelstudio). O painel de consumo da casa lê Alibaba pelo
LOG DO CLI do Qwen — e o copiloto não usa CLI, chama a API direto. Resultado: o
único provider que hoje sustenta produção sumiu do painel de custo.

Journald não serve de fonte: o copiloto-api loga `modelstudio_provider_call`,
mas o Inspector não loga nada (65 análises em 08/09, zero linhas de
provider_call). E o log de systemd é rotativo, então nem histórico ele dá.

Esta tabela é o registro de verdade: uma linha por chamada de LLM, gravada pelo
ContabilProvider — que envelopa o retorno de get_llm_provider() e portanto vale
pra QUALQUER provider (cli, codex, modelstudio, api, ollama). Trocar de provider
de novo não cega o painel outra vez.

`servico` diz quem chamou (api, inspector, cadencia...), porque a conta de
"quanto custa operar" é diferente por serviço e era exatamente isso que faltava.

Migration 100% ADITIVA: cria uma tabela nova, nenhum ALTER em tabela existente.

Revision ID: 0079
Revises: 0078
Create Date: 2026-09-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0079"
down_revision: Union[str, None] = "0078"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_chamadas",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("criado_em", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("servico", sa.Text, nullable=False),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("modelo", sa.Text, nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("duracao_ms", sa.Integer, nullable=True),
        sa.Column("erro", sa.Text, nullable=True),
    )
    # O coletor sempre pergunta "o que rodou desde X" — a janela é o acesso.
    op.create_index("ix_llm_chamadas_criado_em", "llm_chamadas", ["criado_em"])
    op.create_index("ix_llm_chamadas_servico_criado",
                    "llm_chamadas", ["servico", "criado_em"])


def downgrade() -> None:
    op.drop_index("ix_llm_chamadas_servico_criado", table_name="llm_chamadas")
    op.drop_index("ix_llm_chamadas_criado_em", table_name="llm_chamadas")
    op.drop_table("llm_chamadas")
