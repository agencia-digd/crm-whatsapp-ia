"""garantia do remarketing: histórico de configuração, registro de tick, conferência e status órfão

Quatro tabelas NOVAS, todas `IF NOT EXISTS`. Nenhuma coluna alterada, nenhuma
linha tocada: publicar isto não muda o que o motor faz.

━━ config_operacional_historico ━━

`config_operacional` é UPSERT: cada gravação apaga o valor anterior, e "voltar
ao padrão" apagava a linha inteira. "Quem desligou o disparo, e quando?" só se
respondia enquanto o journal existisse. Uma linha por mudança (quem, quando,
de, para), gravada na mesma transação (`config_operacional._historico`).

━━ remarketing_tick_log ━━

Uma linha por volta do carrossel: estava ligado? por que parou? quanto podia,
quanto mandou. É o que deixa responder, sem ler log, "o robô rodou o dia
inteiro ligado e não mandou nada" e "o robô não rodou". ~200 linhas por dia.

━━ remarketing_conferencia ━━

O fechamento de cada dia (`remarketing_conferente`): previstos, aceitos,
entregues, lidos, falhas, respostas por tipo, alarmes. Uma linha por dia de
São Paulo; reconferir o mesmo dia atualiza a linha (o status de entrega chega
com atraso).

━━ whatsapp_status_orfao ━━

Status de entrega que a Meta mandou pra um `wamid` que não está em
`mensagens_whatsapp`. Antes o UPDATE não pegava linha nenhuma e o webhook
respondia "ok": o status sumia. Agora fica aqui e o conferente reaplica quando
a mensagem aparecer.

━━ O DOWNGRADE APAGA ━━

Diferente da 0088: estas tabelas nasceram aqui e só guardam registro — nenhum
interruptor mora nelas.

Revision ID: 0090
Revises: 0089
Create Date: 2026-10-05
"""
from __future__ import annotations

from alembic import op

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None

# O DDL mora numa lista pra o conftest da suíte usar o MESMO texto
# (`tests/conftest.py` importa `DDL`): tabela de teste diferente da de
# produção é teste verde com produção quebrada.
DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS config_operacional_historico (
        id            bigserial PRIMARY KEY,
        chave         text NOT NULL,
        acao          text NOT NULL,
        valor_antes   jsonb,
        valor_depois  jsonb,
        autor         text,
        em            timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_config_hist_chave_em "
    "ON config_operacional_historico (chave, em DESC)",
    """
    CREATE TABLE IF NOT EXISTS remarketing_tick_log (
        id            bigserial PRIMARY KEY,
        tenant_id     integer NOT NULL,
        em            timestamptz NOT NULL DEFAULT now(),
        ligado        boolean,
        motivo_parada text,
        cap_do_dia    integer,
        cota          integer,
        ja_hoje       integer,
        disponiveis   integer,
        enviados      integer NOT NULL DEFAULT 0,
        falhas        integer NOT NULL DEFAULT 0,
        incertos      integer NOT NULL DEFAULT 0,
        detalhe       jsonb
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_rmk_tick_log_tenant_em "
    "ON remarketing_tick_log (tenant_id, em DESC)",
    """
    CREATE TABLE IF NOT EXISTS remarketing_conferencia (
        tenant_id         integer NOT NULL,
        dia               date NOT NULL,
        dados             jsonb NOT NULL,
        alarmes           jsonb NOT NULL DEFAULT '[]'::jsonb,
        conferido_em      timestamptz NOT NULL DEFAULT now(),
        aviso_enviado_em  timestamptz,
        PRIMARY KEY (tenant_id, dia)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS whatsapp_status_orfao (
        id           bigserial PRIMARY KEY,
        wamid        text NOT NULL,
        linha        text,
        status       text,
        patch        jsonb NOT NULL,
        recebido_em  timestamptz NOT NULL DEFAULT now(),
        aplicado_em  timestamptz
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_status_orfao_pendente "
    "ON whatsapp_status_orfao (wamid) WHERE aplicado_em IS NULL",
)


def upgrade() -> None:
    for stmt in DDL:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS whatsapp_status_orfao")
    op.execute("DROP TABLE IF EXISTS remarketing_conferencia")
    op.execute("DROP TABLE IF EXISTS remarketing_tick_log")
    op.execute("DROP TABLE IF EXISTS config_operacional_historico")
