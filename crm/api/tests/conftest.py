"""Setup central para os testes que precisam de um DB Postgres real.

SEGURANÇA (fail-closed): a suíte SÓ roda com `TEST_DATABASE_URL` apontando pra
um banco de teste. Sem a env, ou com ela apontando pro Postgres de produção
(:5432), o conftest LEVANTA no import — a suíte inteira aborta antes de abrir
qualquer conexão. Ver bloco "TRAVA DE BANCO" abaixo.

Quando válida, redireciona o app pro DB de teste, monta o schema
(`create_all` + `_create_schema_gaps`) e semeia o mínimo que os testes de
endpoint esperam via `identify_sender`:
  - 3 corretoras ativas (id=1 Pessoa5, id=2 Pessoa1, id=3 Pessoa3 — mesma
    numeração de painel.py) com telefone_normalizado, papel, kommo_user_id
    e evolution_instance
  - 1 interno/dona (Pessoa) na tabela internos_acme

Como rodar (VPS — banco de teste dedicado já provisionado no cluster 16-oficial,
porta 5433, separado do cluster de aplicação :5432):

    cd /opt/crm
    TEST_DATABASE_URL=postgresql+psycopg://copiloto_test:contato@example.com:5433/copiloto_test \\
        .venv/bin/python -m pytest tests/ -q

Recriar o banco de teste do zero (idempotente):

    sudo -u postgres psql -p 5433 -c "DROP DATABASE IF EXISTS copiloto_test;"
    sudo -u postgres psql -p 5433 -c "DO \\$\\$ BEGIN CREATE ROLE copiloto_test LOGIN PASSWORD 'copiloto_test'; EXCEPTION WHEN duplicate_object THEN NULL; END \\$\\$;"
    sudo -u postgres psql -p 5433 -c "CREATE DATABASE copiloto_test OWNER copiloto_test;"

O schema é montado pela fixture `_prepare_test_db` a cada sessão de pytest —
não precisa rodar migration à mão.

Alternativa efêmera (Docker, se preferir descartar o banco a cada run):
    docker run -d --name copiloto_test_db -e POSTGRES_USER=copiloto_test \\
        -e POSTGRES_PASSWORD=copiloto_test -e POSTGRES_DB=copiloto_test \\
        -p 15432:5432 postgres:16
    TEST_DATABASE_URL=postgresql+psycopg://copiloto_test:contato@example.com:15432/copiloto_test
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

# ── TRAVA DE SEGURANÇA (roda ANTES de qualquer import de `app`) ──────────────
# Nenhum teste pode enviar mensagem REAL (WhatsApp). Duas camadas:
#   1. COPILOTO_UNDER_PYTEST=1 → hard-block em EvolutionClient.send_text
#      (app/integrations/evolution_client.py::_blocked_under_pytest). Vale pra
#      sessão inteira e resiste a entregas TARDIAS em background que disparam
#      DEPOIS do teste terminar (ver _deliver_late_response em api/assistente.py —
#      foi exatamente esse caminho que vazou "tarde demais").
#   2. DRY_RUN=true → corta o envio na origem (settings.dry_run é lido em
#      call-time pelos caminhos de entrega). Sobrescreve o DRY_RUN=false do
#      .env de produção — env var tem prioridade sobre .env no pydantic-settings.
# Sem isso, rodar a suíte contra o ambiente de produção vaza mensagem mock pro
# WhatsApp real (incidentes 04/05/2026 "DRYRUN_FORMAT_CHECK" e 11/07/2026
# "tarde demais" pra Pessoa Pessoa9 Acme). Ver CLAUDE.md regra #6/#9.
os.environ["COPILOTO_UNDER_PYTEST"] = "1"
os.environ["DRY_RUN"] = "true"

# ── TRAVA DE BANCO (fail-closed, import-time) ────────────────────────────────
# Até 26/07/2026 este bloco era `if _TEST_DB:` — ou seja, SEM a env a suíte
# simplesmente caía no DATABASE_URL do .env, que é o Postgres de PRODUÇÃO.
# Testes com `DELETE FROM coaching_observations WHERE corretora_id IN (1,2,3)`
# e `DELETE FROM mensagens_automacao WHERE corretora_id IN (1,2,3)` rodavam
# contra os dados reais de Pessoa5/Pessoa1/Pessoa3. Hoje as tabelas estão
# vazias — na semana que vem, com as 3 no painel, um `pytest` apagaria tudo
# sem backup e sem log.
#
# Escolha de arquitetura: FAIL-CLOSED, não "default seguro implícito".
#   - Não usamos pytest-env (`env = [...]` no pyproject): ele falharia ABERTO
#     se o banco de teste não estivesse de pé E arpessoa a fixture de seed
#     abaixo (que faz `DELETE FROM internos_acme` / `DELETE FROM corretoras`
#     SEM WHERE) contra qualquer URL digitada errada. Hoje essa fixture é
#     inerte pelo early-return; armá-la seria mais arriscado que o status quo.
#   - Não inferimos "um banco de teste qualquer" sozinhos: a URL tem que ser
#     explícita, e é validada contra o padrão de produção. Sem dependência nova
#     e sem caminho silencioso pro banco errado.
#
# A trava roda em IMPORT-TIME (não em fixture): fixture roda depois que os
# módulos de teste já importaram `app.db.session`, e nesse ponto o `engine` /
# `SessionLocal` já foi resolvido com a URL errada.
_TEST_DB = os.environ.get("TEST_DATABASE_URL", "").strip()

_MSG_COMO_RODAR = (
    "\n\nComo rodar a suíte:\n"
    "  TEST_DATABASE_URL=postgresql+psycopg://copiloto_test:copiloto_test"
    "@127.0.0.1:5433/copiloto_test \\\n"
    "      .venv/bin/python -m pytest tests/ -q\n"
    "(banco de teste dedicado no cluster 16-oficial :5433 — ver docstring "
    "de tests/conftest.py pra recriar do zero)"
)

if not _TEST_DB:
    raise RuntimeError(
        "TEST_DATABASE_URL não está setada — a suíte se recusa a rodar. "
        "Sem ela o app cairia no DATABASE_URL do .env, que é o Postgres de "
        "PRODUÇÃO, e há testes que fazem DELETE em tabelas reais."
        + _MSG_COMO_RODAR
    )


def _aponta_pra_producao(url: str) -> bool:
    """True quando a URL bate no cluster de aplicação (Postgres 16-main :5432).

    Produção do copiloto vive em `postgresql+psycopg://user:CHANGE_ME@
    localhost:5432/copiloto`. A porta 5432 é o cluster de APLICAÇÃO inteiro
    (copiloto e vizinhos) — qualquer banco lá dentro é dado real, então o
    critério é a porta, não o nome do banco (que pode ser digitado errado).
    O banco de teste vive no cluster 16-oficial, porta 5433.
    """
    partes = urlsplit(url)
    porta = partes.port
    if porta is None:
        # Sem porta explícita o libpq assume 5432 → produção.
        return True
    return porta == 5432


if _aponta_pra_producao(_TEST_DB):
    raise RuntimeError(
        "TEST_DATABASE_URL aponta pro Postgres de PRODUÇÃO (porta 5432). "
        "A suíte faz DELETE/UPDATE em tabelas reais e se recusa a rodar aí. "
        "Use o banco de teste dedicado."
        + _MSG_COMO_RODAR
    )

os.environ["DATABASE_URL"] = _TEST_DB
os.environ.setdefault("COPILOTO_INTERNAL_TOKEN", "test-internal-token")
# Sem isso, settings.evolution_webhook_secret fica "" (default) e a URL
# /webhook/evolution/assistente/{secret} vira /webhook/evolution/assistente/ (segmento
# vazio) — FastAPI não casa a rota e devolve 404 em vez de rodar o handler
# (que os testes de test_evolution_webhook.py esperam ver retornar 200).
os.environ.setdefault("EVOLUTION_WEBHOOK_SECRET", "test-webhook-secret")

# ── PIN DE ENV DE NEGÓCIO (import-time, mesma janela da trava) ───────────────
# Estas flags vivem no `.env` de produção e a Dona mexe nelas no dia a dia
# ("hoje só a Pessoa11 atende"). Como env var tem precedência sobre .env no
# pydantic-settings, fixá-las AQUI faz a suíte parar de mudar de cor conforme o
# estado operacional — que já derrubou testes três vezes:
#   LEAD_INTAKE_ATIVO=true  → 3 testes de test_lead_intake vermelhos E o intake
#                             ESCREVENDO lead de verdade;
#   ROLETA_CORRETORAS=[2]   → test_cadencia_d0_origem vermelho;
#   INTAKE_META_PAGE_ID     → test_meta_leadgen dependendo da página real.
# Valores = os DEFAULTS DE PROJETO declarados em app/config.py (não inventados
# aqui). Teste que precise de outro valor usa `patch.object(settings, ...)`.
os.environ["LEAD_INTAKE_ATIVO"] = "false"
os.environ["ROLETA_CORRETORAS"] = "[1, 2]"
os.environ["INTAKE_META_PAGE_ID"] = ""
# CADENCIA_CAP_POR_CORRETORA={"1":200,"2":200,"3":25} em produção (Pessoa12 apertada,
# Pessoa10/Pessoa11 com número quente). Em teste fica VAZIO: quem exercita cap ajusta
# `cadencia_cap_diario` e espera esse número valer. Sem isto, o mapa de produção
# sobrepõe o valor do teste e o cap "não é atingido" — 5 testes vermelhos.
# (dict vindo do .env NÃO é sobreposto por env var vazia no pydantic-settings —
# então o pin de verdade é feito no objeto settings, logo abaixo do import.)

import pytest

# Seed de corretoras — ORDEM IMPORTA: `corretoras.id` é SERIAL, e a convenção
# canônica do produto (painel.py: CORRETORA_LABEL/CORRETORA_KOMMO_USER) fixa
# id=1 Pessoa5, id=2 Pessoa1, id=3 Pessoa3. Vários testes dependem dessa
# numeração exata (ex: CORRETORA_A_ID=3 em test_consolidacao_metricas.py,
# corretora_id=1 é Pessoa5 em test_briefing.py). A ordem alfabética antiga
# (Pessoa3 primeiro) invertia os ids e quebrava esses testes.
#
# papel: só Pessoa3 foge do default do model (`corretora_solo`) — ela é
# "corretora_atendedora_de_dona" (test_sender_lookup.py::test_corretora_a_eh_corretora).
#
# kommo_user_id: mesmos valores de `painel.py:CORRETORA_KOMMO_USER` (fonte
# única já existente no código, não inventado aqui).
#
# evolution_instance: nome real da instância Evolution de cada corretora
# (Pessoa3="acme_corretora_a" é usado por app/api/cloud_corretora_a.py e
# app/tools/evolution.py — inclusive por EvolutionTool.send_to_self, que
# busca a corretora pela instância).
#
# `id` é setado EXPLICITAMENTE (não confia na ordem de INSERT + SERIAL): o
# container Postgres de teste pode sobreviver entre execuções (volume do
# docker-compose) — se a sequência de `corretoras.id` já tiver avançado de uma
# run anterior, inserir sem `id` explícito geraria 4/5/6 em vez de 1/2/3 e
# quebraria de novo os testes que dependem desses ids fixos. A sequência é
# realinhada (`setval`) depois do insert pra não colidir com esses ids em
# inserts futuros (SERIAL normal, sem id explícito) feitos pelos testes.
# Telefones do seed — EXPORTADOS de propósito. Os testes que batem no DB real
# (test_sender_lookup, test_endpoint_owns_phone, test_endpoint_assistente_run)
# devem importar estas constantes em vez de repetir o literal: quando o número
# de uma corretora muda (a Pessoa12 já trocou +5500000000000 → +5500000000000), muda-se
# UM lugar e a suíte acompanha. Testes que montam SQLite próprio NÃO usam isto
# (têm o seed deles) e test_phone.py usa +5500000000000 DE PROPÓSITO como fixture
# de telefone FIXO — nenhum dos dois deve ser tocado por search-replace.
TEL_CORRETORA_C = "+5500000000000"
TEL_CORRETORA_B = "+5500000000000"
TEL_CORRETORA_A = "+5500000000000"
TEL_DONA = "+5500000000000"
# Números que NÃO estão em corretoras nem em internos_acme — usados pelos
# testes de "unknown"/404. Dona saiu de internos_acme na Onda P1.
TEL_DANIELA_REMOVIDA = "+5500000000000"
TEL_DESCONHECIDO = "+5500000000000"

_CORRETORAS = [
    # (id, nome, telefone_normalizado, papel, kommo_user_id, evolution_instance)
    (1, "Pessoa5", TEL_CORRETORA_C, "corretora_solo", 10000006, "acme_corretora_c"),
    (2, "Pessoa1", TEL_CORRETORA_B, "corretora_solo", 10000007, "acme_corretor"),
    (3, "Pessoa3", TEL_CORRETORA_A, "corretora_atendedora_de_dona", 10000003, "acme_corretora_a"),
]
_DONA = ("Pessoa Pessoa9 Acme", TEL_DONA)


# ── Tabelas/views que só nascem via migration (não são models SQLAlchemy) ────
#
# `Base.metadata.create_all(engine)` só cria o que está em `app/db/models.py`.
# Várias tabelas do projeto foram criadas direto por migration Alembic (raw
# `op.create_table`) sem nunca ganhar uma classe ORM correspondente — pra
# create_all elas simplesmente não existem, e qualquer teste que bata nelas
# via SQL cru (`INSERT INTO ligacoes_semanais ...`) falha com
# "relation does not exist".
#
# Cada bloco abaixo é uma transcrição 1:1 do DDL final da(s) migration(s) que
# criaram/alteraram a tabela (ver app/db/migrations/versions/), incluindo
# ALTERs posteriores (ex: visitas ganhou is_revisita em 0026 e lead_id/origem
# em 0037; atendimentos_evolution_mensais ganhou reengajados_* em 0029).
# `tenant_id` é incluído só nas tabelas listadas em `_CORE_TABLES` da migration
# 0042 (as de fora — stage_map, telegram_updates_processed — não têm tenant_id
# em produção).
#
# Tudo idempotente (IF NOT EXISTS / DO+EXCEPTION duplicate_object) porque a
# fixture roda 1x por sessão de pytest mas o container Postgres de teste pode
# sobreviver entre execuções (volume do docker-compose) — rodar 2x não pode
# quebrar.
_MISSING_TABLES_DDL: list[str] = [
    # config_operacional — 0088. Existia em produção criada por fora, sem
    # migration: num banco novo, todo teste que grava config pela tela dava
    # erro de tabela inexistente (31 erros na suíte de 05/10/2026).
    """
    CREATE TABLE IF NOT EXISTS config_operacional (
        chave          TEXT PRIMARY KEY,
        valor          JSONB NOT NULL,
        atualizado_em  TIMESTAMPTZ NOT NULL DEFAULT now(),
        atualizado_por TEXT
    )
    """,
    # 0a) mensagens_whatsapp.fone8 — 0085 (coluna gerada + índice). Existia em
    # produção criada à mão; o bloco "retomar" do Meu dia casa por ela.
    r"""
    ALTER TABLE mensagens_whatsapp
      ADD COLUMN IF NOT EXISTS fone8 text GENERATED ALWAYS AS
      (right(regexp_replace(coalesce(contato_telefone, ''), '\D', '', 'g'), 8)) STORED
    """,
    "CREATE INDEX IF NOT EXISTS ix_msg_fone8_ts "
    "ON mensagens_whatsapp (fone8, timestamp_evolution DESC)",
    # 0a') etapas da roda do remarketing — Fila (23), Disparado (24) e
    # Respondeu (25), criadas à mão em produção em 26/07. Num banco zerado elas
    # não existiam e 42 testes do carrossel caíam em FK (`leads_stage_id_fkey`)
    # — só passavam em banco reaproveitado, onde outro teste já as tinha criado.
    """
    INSERT INTO pipeline_stages (id, tenant_id, nome, funil, ordem)
    SELECT v.id, 1, v.nome, 'remarketing', v.ordem
      FROM (VALUES (23, 'Fila', 7), (24, 'Disparado', 8), (25, 'Respondeu', 9))
           AS v(id, nome, ordem)
     WHERE NOT EXISTS (SELECT 1 FROM pipeline_stages p WHERE p.id = v.id)
    """,
    # 0b) etapa "Não perturbe" — 0077. Opt-out não é venda perdida: quem pediu
    # silêncio some do funil sem virar recusa comercial na conta.
    """
    INSERT INTO pipeline_stages (id, tenant_id, nome, funil, ordem, tipo)
    SELECT 26, 1, 'Não perturbe', 'principal', 99, 'perdido'
     WHERE NOT EXISTS (SELECT 1 FROM pipeline_stages WHERE id = 26)
    """,
    # 0b') As etapas acima entram com id EXPLÍCITO, o que não anda a sequência:
    # sem isto, o teste que cria etapa com id automático bate em "id 23 já
    # existe" quando a sequência chega lá.
    """
    SELECT setval(pg_get_serial_sequence('pipeline_stages', 'id'),
                  GREATEST((SELECT COALESCE(max(id), 1) FROM pipeline_stages
                             WHERE id < 9000), 26))
    """,
    # 0) recado_corretora — 0076 (recado avulso entregue no clique do botão)
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
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ix_recado_corretora_pendente
        ON recado_corretora (tenant_id, corretora_id)
     WHERE enviado_em IS NULL AND cancelado_em IS NULL
    """,
    # 0c) visita_lembrete_d1 — 0086 (lembrete de véspera da visita, pra corretora)
    """
    CREATE TABLE IF NOT EXISTS visita_lembrete_d1 (
        id                  SERIAL PRIMARY KEY,
        tenant_id           INTEGER NOT NULL DEFAULT 1,
        visita_id           BIGINT  NOT NULL,
        corretora_id        INTEGER,
        data_visita         DATE    NOT NULL,
        ordem_no_lote       INTEGER NOT NULL,
        telefone_corretora  TEXT    NOT NULL,
        lead_nome           TEXT,
        canal               TEXT,
        enviado_em          TIMESTAMPTZ,
        desfecho            TEXT,
        desfecho_em         TIMESTAMPTZ,
        resposta_texto      TEXT,
        sem_resposta_em     TIMESTAMPTZ,
        criado_em           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT ck_lembrete_d1_desfecho CHECK (
            desfecho IS NULL OR desfecho IN
            ('confirmada','remarcada','cancelada','nao_entendido')),
        CONSTRAINT uq_lembrete_d1_visita UNIQUE (tenant_id, visita_id)
    )
    """,
    # 1) daily_metrics_corretora — 0008 + 0017 (colunas de funil)
    """
    CREATE TABLE IF NOT EXISTS daily_metrics_corretora (
        data DATE NOT NULL,
        corretora_id INTEGER NOT NULL REFERENCES corretoras(id),
        total_leads_mes INTEGER,
        atendidos_mes INTEGER,
        tarefas_atrasadas INTEGER,
        fontes_completas TEXT[] NOT NULL DEFAULT '{}',
        fontes_falhas TEXT[] NOT NULL DEFAULT '{}',
        gerado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
        entrou_mes INTEGER,
        em_atendimento_mes INTEGER,
        agendados_mes INTEGER,
        visitas_realizadas_mes INTEGER,
        vendas_kommo_mes INTEGER,
        vgv_kommo_mes NUMERIC(14,2),
        tenant_id INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (data, corretora_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_daily_metrics_corretora "
    "ON daily_metrics_corretora (corretora_id, data DESC)",

    # 2) vendas — 0009
    """
    CREATE TABLE IF NOT EXISTS vendas (
        id BIGSERIAL PRIMARY KEY,
        corretora_id INTEGER NOT NULL REFERENCES corretoras(id),
        cliente_nome TEXT NOT NULL,
        valor NUMERIC(14,2) NOT NULL,
        data_venda DATE NOT NULL,
        observacao TEXT,
        registrado_por_telefone TEXT,
        registrado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
        tenant_id INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT ck_vendas_valor_nonneg CHECK (valor >= 0)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_vendas_corretora_data ON vendas (corretora_id, data_venda DESC)",
    "CREATE INDEX IF NOT EXISTS idx_vendas_data ON vendas (data_venda DESC)",
    # 0084 — origem da venda (só `midia` entra no ROI)
    "ALTER TABLE vendas ADD COLUMN IF NOT EXISTS origem TEXT NOT NULL DEFAULT 'nao_informada'",
    "ALTER TABLE vendas DROP CONSTRAINT IF EXISTS ck_vendas_origem",
    "ALTER TABLE vendas ADD CONSTRAINT ck_vendas_origem CHECK "
    "(origem IN ('midia', 'indicacao', 'parceiro', 'nao_informada'))",

    # 3) ligacoes_semanais — 0010
    """
    CREATE TABLE IF NOT EXISTS ligacoes_semanais (
        id BIGSERIAL PRIMARY KEY,
        corretora_id INTEGER NOT NULL REFERENCES corretoras(id),
        ano_semana_iso TEXT NOT NULL,
        ligacoes INTEGER NOT NULL DEFAULT 0,
        atendeu INTEGER NOT NULL DEFAULT 0,
        prosseguiu INTEGER NOT NULL DEFAULT 0,
        registrado_por_telefone TEXT,
        registrado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
        atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
        tenant_id INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_ligacoes_corretora_semana UNIQUE (corretora_id, ano_semana_iso),
        CONSTRAINT ck_ligacoes_atendeu_le_ligacoes CHECK (atendeu <= ligacoes),
        CONSTRAINT ck_ligacoes_pross_le_atendeu CHECK (prosseguiu <= atendeu)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ligacoes_semana ON ligacoes_semanais (ano_semana_iso)",

    # 4) agendamentos_semanais — 0011
    """
    CREATE TABLE IF NOT EXISTS agendamentos_semanais (
        id BIGSERIAL PRIMARY KEY,
        corretora_id INTEGER NOT NULL REFERENCES corretoras(id),
        ano_semana_iso TEXT NOT NULL,
        agendados INTEGER NOT NULL DEFAULT 0,
        realizados INTEGER NOT NULL DEFAULT 0,
        registrado_por_telefone TEXT,
        registrado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
        atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
        tenant_id INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_agendamentos_corretora_semana UNIQUE (corretora_id, ano_semana_iso),
        CONSTRAINT ck_agendamentos_real_le_agend CHECK (realizados <= agendados)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agendamentos_semana ON agendamentos_semanais (ano_semana_iso)",

    # 5) visitas — 0012 + 0026 (is_revisita) + 0037 (lead_id/origem/dedup)
    """
    CREATE TABLE IF NOT EXISTS visitas (
        id BIGSERIAL PRIMARY KEY,
        corretora_id INTEGER NOT NULL REFERENCES corretoras(id),
        evento_calendar_id TEXT UNIQUE,
        lead_nome TEXT NOT NULL,
        data_hora TIMESTAMPTZ NOT NULL,
        bairro TEXT,
        telefone_lead TEXT,
        status TEXT NOT NULL DEFAULT 'agendada',
        criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
        atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
        criado_por_telefone TEXT,
        status_atualizado_por_tel TEXT,
        is_revisita BOOLEAN NOT NULL DEFAULT false,
        lead_id TEXT,
        origem TEXT,
        tenant_id INTEGER NOT NULL DEFAULT 1,
        -- espelha o CHECK de PRODUÇÃO (conferido em 05/08/2026): sem
        -- 'pendente_confirmacao' aqui, o banco de teste recusava um status que
        -- existe de verdade lá, e teste que não consegue reproduzir o dado real
        -- é teste que não protege nada.
        CONSTRAINT ck_visitas_status CHECK (status IN ('agendada','compareceu','no_show','cancelada','pendente_confirmacao'))
    )
    """,
    # O CREATE acima só vale pra banco NOVO — quem já tem a tabela ficou com o
    # CHECK antigo (sem 'pendente_confirmacao') pra sempre. Este ALTER
    # idempotente alinha o banco de teste existente com produção.
    "ALTER TABLE visitas DROP CONSTRAINT IF EXISTS ck_visitas_status",
    "ALTER TABLE visitas ADD CONSTRAINT ck_visitas_status CHECK "
    "(status IN ('agendada','compareceu','no_show','cancelada','pendente_confirmacao'))",
    "CREATE INDEX IF NOT EXISTS idx_visitas_corretora_data ON visitas (corretora_id, data_hora DESC)",
    "CREATE INDEX IF NOT EXISTS idx_visitas_status ON visitas (status)",
    "CREATE INDEX IF NOT EXISTS idx_visitas_revisita ON visitas (is_revisita)",
    "CREATE INDEX IF NOT EXISTS idx_visitas_lead_id ON visitas (lead_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_visitas_corretora_datahora_leadnome "
    "ON visitas (corretora_id, data_hora, lower(lead_nome)) WHERE status <> 'cancelada'",

    # 6) notas — 0013
    """
    CREATE TABLE IF NOT EXISTS notas (
        id BIGSERIAL PRIMARY KEY,
        contexto TEXT NOT NULL,
        texto TEXT NOT NULL,
        registrado_por_telefone TEXT,
        registrado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
        tenant_id INTEGER NOT NULL DEFAULT 1
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_notas_contexto_data ON notas (contexto, registrado_em DESC)",

    # 7) vendas_mensais — 0023
    """
    CREATE TABLE IF NOT EXISTS vendas_mensais (
        id SERIAL PRIMARY KEY,
        corretora_id INTEGER NOT NULL REFERENCES corretoras(id),
        ano_mes CHAR(7) NOT NULL,
        qtde_vendas INTEGER NOT NULL DEFAULT 0,
        vgv NUMERIC(14,2) NOT NULL DEFAULT 0,
        qtde_leads INTEGER,
        qtde_atendidos INTEGER,
        qtde_agendados INTEGER,
        qtde_visitas INTEGER,
        fonte TEXT,
        criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
        atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
        tenant_id INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_vendas_mensais_corretora_mes UNIQUE (corretora_id, ano_mes),
        CONSTRAINT ck_vendas_mensais_qtde_nonneg CHECK (qtde_vendas >= 0),
        CONSTRAINT ck_vendas_mensais_vgv_nonneg CHECK (vgv >= 0)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_vendas_mensais_corretora_mes "
    "ON vendas_mensais (corretora_id, ano_mes DESC)",

    # 8) atendimentos_evolution_mensais — 0028 + 0029 (reengajados, dropa
    #    ck_atend_evo_le_leads original — NÃO recriar, ela não existe mais)
    """
    CREATE TABLE IF NOT EXISTS atendimentos_evolution_mensais (
        id SERIAL PRIMARY KEY,
        corretora_id INTEGER NOT NULL REFERENCES corretoras(id),
        ano_mes CHAR(7) NOT NULL,
        leads_novos INTEGER NOT NULL DEFAULT 0,
        atendidos_2via INTEGER NOT NULL DEFAULT 0,
        so_corretora_falou INTEGER NOT NULL DEFAULT 0,
        sem_msg INTEGER NOT NULL DEFAULT 0,
        tel_invalido INTEGER NOT NULL DEFAULT 0,
        atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
        reengajados_total INTEGER NOT NULL DEFAULT 0,
        reengajados_2via INTEGER NOT NULL DEFAULT 0,
        tenant_id INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_atend_evo_corretora_mes UNIQUE (corretora_id, ano_mes),
        CONSTRAINT ck_atend_evo_nonneg CHECK (atendidos_2via >= 0),
        CONSTRAINT ck_atend_evo_reeng_nonneg CHECK (
            reengajados_2via >= 0 AND reengajados_total >= 0
            AND reengajados_2via <= reengajados_total
        )
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_atend_evo_corretora_mes "
    "ON atendimentos_evolution_mensais (corretora_id, ano_mes DESC)",

    # 9) observacao_corretora — 0033
    """
    CREATE TABLE IF NOT EXISTS observacao_corretora (
        id BIGSERIAL PRIMARY KEY,
        lead_id TEXT NOT NULL,
        corretora_id INTEGER REFERENCES corretoras(id) ON DELETE SET NULL,
        autor TEXT,
        texto TEXT NOT NULL,
        ia_nota_kommo TEXT,
        ia_temperatura TEXT,
        ia_descartar BOOLEAN NOT NULL DEFAULT false,
        ia_motivo TEXT,
        kommo_note_ok BOOLEAN NOT NULL DEFAULT false,
        criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
        tenant_id INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT ck_observacao_corretora_temperatura CHECK (
            ia_temperatura IS NULL OR ia_temperatura IN ('frio','morno','quente')
        )
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_observacao_corretora_lead ON observacao_corretora (lead_id)",
    "CREATE INDEX IF NOT EXISTS ix_observacao_corretora_cid_lead "
    "ON observacao_corretora (corretora_id, lead_id)",

    # 10) stage_map — 0036. SEM tenant_id (config global, fora da 0042).
    """
    CREATE TABLE IF NOT EXISTS stage_map (
        id SERIAL PRIMARY KEY,
        kommo_status_id TEXT,
        twenty_stage TEXT NOT NULL,
        label_pt TEXT NOT NULL,
        ordem INTEGER NOT NULL DEFAULT 0,
        ativo BOOLEAN NOT NULL DEFAULT true,
        CONSTRAINT uq_stage_map_twenty_stage UNIQUE (twenty_stage)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_stage_map_kommo_status_id "
    "ON stage_map (kommo_status_id) WHERE kommo_status_id IS NOT NULL",

    # 11) telegram_updates_processed — 0045. SEM tenant_id (dedup técnico).
    """
    CREATE TABLE IF NOT EXISTS telegram_updates_processed (
        update_id BIGINT PRIMARY KEY,
        chat_id BIGINT,
        message_id BIGINT,
        claimed_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_telegram_updates_processed_claimed_at "
    "ON telegram_updates_processed (claimed_at)",

    # 12) conversa_lida — 0092. "Lido até" do Chat por login + conversa (p8).
    """
    CREATE TABLE IF NOT EXISTS conversa_lida (
        usuario TEXT NOT NULL,
        p8 TEXT NOT NULL,
        lido_ate TIMESTAMPTZ NOT NULL,
        atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (usuario, p8)
    )
    """,

    # ── Views — 0014 (dependem de vendas/ligacoes_semanais/agendamentos_semanais
    #    já criadas acima). CREATE OR REPLACE já é idempotente por natureza.
    """
    CREATE OR REPLACE VIEW vw_metricas_mes_corretora AS
    SELECT
      c.id AS corretora_id,
      c.nome AS corretora_nome,
      to_char(d::date, 'YYYY-MM') AS mes,
      d::date AS mes_data,
      COALESCE((SELECT COUNT(*) FROM vendas v
                WHERE v.corretora_id = c.id
                  AND to_char(v.data_venda, 'YYYY-MM') = to_char(d::date, 'YYYY-MM')
               ), 0) AS vendas_mes,
      COALESCE((SELECT SUM(valor) FROM vendas v
                WHERE v.corretora_id = c.id
                  AND to_char(v.data_venda, 'YYYY-MM') = to_char(d::date, 'YYYY-MM')
               ), 0) AS vgv_mes
    FROM corretoras c
    CROSS JOIN generate_series(
      date_trunc('month', CURRENT_DATE - INTERVAL '6 months'),
      date_trunc('month', CURRENT_DATE),
      '1 month'
    ) AS d
    WHERE c.ativo = true
    """,
    """
    CREATE OR REPLACE VIEW vw_semana_atual_corretora AS
    SELECT
      c.id AS corretora_id,
      c.nome AS corretora_nome,
      to_char(CURRENT_DATE, 'IYYY-"W"IW') AS ano_semana_iso,
      COALESCE(l.ligacoes, 0) AS ligacoes,
      COALESCE(l.atendeu, 0) AS ligacoes_atendeu,
      COALESCE(l.prosseguiu, 0) AS ligacoes_prosseguiu,
      COALESCE(a.agendados, 0) AS agendados,
      COALESCE(a.realizados, 0) AS realizados,
      l.atualizado_em AS ligacoes_atualizado_em,
      a.atualizado_em AS agendamentos_atualizado_em
    FROM corretoras c
    LEFT JOIN ligacoes_semanais l
      ON l.corretora_id = c.id
      AND l.ano_semana_iso = to_char(CURRENT_DATE, 'IYYY-"W"IW')
    LEFT JOIN agendamentos_semanais a
      ON a.corretora_id = c.id
      AND a.ano_semana_iso = to_char(CURRENT_DATE, 'IYYY-"W"IW')
    WHERE c.ativo = true
    """,

    # ── Constraints/índices que faltam em tabelas JÁ modeladas (models.py não
    #    é espelho 1:1 do DDL das migrations — só ganhou tenant_id em 0042,
    #    CHECK/índices nomeados de create_table nunca foram retroportados).
    #    ADD CONSTRAINT não suporta IF NOT EXISTS no Postgres — usa DO+EXCEPTION.
    """
    -- 0005 criou o CHECK com ('corretora','dona','assistant'); a 0068 abriu
    -- pra 'lead' e 'pessoa' (conversa do lead com a Pessoa na linha da Corretor).
    -- DROP + ADD em vez de DO/EXCEPTION: num banco de teste que já tem o
    -- CHECK antigo, "se existe, ignora" deixaria a versão velha pra sempre.
    ALTER TABLE conversations DROP CONSTRAINT IF EXISTS ck_conversations_sender_kind
    """,
    """
    ALTER TABLE conversations ADD CONSTRAINT ck_conversations_sender_kind
        CHECK (sender_kind = ANY (ARRAY['corretora','dona','assistant','lead','pessoa']::text[]))
    """,
    """
    DO $$ BEGIN
        ALTER TABLE conversations ADD CONSTRAINT ck_conversations_message_role
            CHECK (message_role IN ('user','assistant'));
    EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$
    """,
    "CREATE INDEX IF NOT EXISTS idx_conversations_phone_time "
    "ON conversations (phone, created_at DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_conversations_msgid "
    "ON conversations (message_id) WHERE message_id IS NOT NULL",

    """
    DO $$ BEGIN
        ALTER TABLE coaching_observations ADD CONSTRAINT coaching_obs_categoria_chk
            CHECK (categoria IN (
                'falta_qualificacao','foco_valor_cedo','resposta_seca',
                'perdeu_deixa','demora_resposta','manual_followup_apos_robo','outro'
            ));
    EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$
    """,
    """
    DO $$ BEGIN
        ALTER TABLE coaching_observations ADD CONSTRAINT coaching_obs_origem_chk
            CHECK (origem IN ('lead_quente','lead_esfriou'));
    EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$
    """,
    "CREATE INDEX IF NOT EXISTS idx_coaching_corretora_data "
    "ON coaching_observations (corretora_id, analyzed_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_coaching_categoria ON coaching_observations (categoria)",

    # ── mensagens_whatsapp (0018) — JÁ modelada, mas sem a UNIQUE nomeada que
    #    o código usa em ON CONFLICT ON CONSTRAINT. Sem isso, TODO insert via
    #    gravar_mensagem_evolution_from_webhook/_from_evolution_msg falha com
    #    "there is no unique or exclusion constraint matching the ON CONFLICT
    #    specification" (psycopg.errors.InvalidColumnReference) — engolido pelo
    #    try/except do serviço e logado como mensagens_evolution_gravar_failed,
    #    então a mensagem nunca é gravada e os testes leem `None` de volta.
    """
    DO $$ BEGIN
        ALTER TABLE mensagens_whatsapp ADD CONSTRAINT uq_mensagem_instance
            UNIQUE (evolution_message_id, instance);
    EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$
    """,
    "CREATE INDEX IF NOT EXISTS idx_mensagens_evolution_corretora_ts "
    "ON mensagens_whatsapp (corretora_id, timestamp_evolution DESC)",
    "CREATE INDEX IF NOT EXISTS idx_mensagens_evolution_telefone_ts "
    "ON mensagens_whatsapp (contato_telefone, timestamp_evolution DESC)",

    # ── mensagens_automacao (0016) — mesmo problema: falta a UNIQUE nomeada
    #    que upsert_automacoes() usa em ON CONFLICT (corretora_id, texto_normalizado).
    """
    DO $$ BEGIN
        ALTER TABLE mensagens_automacao ADD CONSTRAINT uq_mensagens_automacao_corretora_texto
            UNIQUE (corretora_id, texto_normalizado);
    EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$
    """,
    "CREATE INDEX IF NOT EXISTS idx_mensagens_automacao_ativas "
    "ON mensagens_automacao (corretora_id, ativa) WHERE ativa = true",

    # ── leads.adotado_ligacao_em — TAG "veio do remarketing por LIGAÇÃO"
    #    (DDL direta, scripts/criar_ligacao_resultado.py). `create_all` só CRIA
    #    tabela: num banco de teste PERSISTENTE, com `leads` já existindo, a
    #    coluna nova nunca apareceria.
    "ALTER TABLE leads ADD COLUMN IF NOT EXISTS adotado_ligacao_em TIMESTAMPTZ",

    # ── corretoras.nome_publico — o nome que o CLIENTE lê (migration 0082).
    #    Mesmo motivo do ALTER acima: `create_all` não altera tabela que já
    #    existe, e o banco de teste sobrevive entre execuções.
    "ALTER TABLE corretoras ADD COLUMN IF NOT EXISTS nome_publico TEXT",

    # ── config_operacional — onde a tela de automações grava (migration 0088).
    #    Existia só em produção, criada por fora: num banco limpo os testes que
    #    provam a gravação pela tela morriam em "relation does not exist" até
    #    alguém criar a tabela à mão. DDL igual ao da 0088, coluna por coluna.
    r"""
    CREATE TABLE IF NOT EXISTS config_operacional (
        chave          text PRIMARY KEY,
        valor          jsonb NOT NULL,
        atualizado_em  timestamptz NOT NULL DEFAULT now(),
        atualizado_por text
    )
    """,
]

# Seed de stage_map (0036) — os 12 stages do pipeline 10000004. ON CONFLICT
# pela unique de twenty_stage torna o INSERT seguro em re-execuções.
_STAGE_MAP_SEED = [
    ("10000008", "ENTRADA", "Entrada", 1),
    ("10000009", "NEW", "Entrou", 2),
    ("10000005", "SCREENING", "Em atendimento", 3),
    ("10000001", "FOLLOWUP", "Follow-up", 4),
    ("10000010", "RECUPERADOS", "Recuperados", 5),
    ("10000011", "PROVISORIA", "Provisória", 6),
    ("10000002", "MEETING", "Visita agendada", 7),
    ("10000012", "VISITA_REAL", "Visita realizada", 8),
    ("10000013", "PROPOSAL", "Proposta", 9),
    ("10000014", "DOCUMENTACAO", "Documentação", 10),
    ("142", "CUSTOMER", "Venda", 11),
    ("143", "PERDIDA", "Perdida", 12),
]


def _ddl_da_migration(arquivo: str) -> tuple[str, ...]:
    """`DDL` de uma migration, carregado pelo caminho do arquivo (o nome
    começa com número, então não é importável como módulo)."""
    import importlib.util
    from pathlib import Path

    caminho = (Path(__file__).resolve().parents[1] / "app" / "db" / "migrations"
               / "versions" / f"{arquivo}.py")
    spec = importlib.util.spec_from_file_location(f"_mig_{arquivo}", caminho)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return tuple(mod.DDL)


def _create_schema_gaps(engine) -> None:
    """Cria, de forma idempotente, o que só existe via migration (ver
    `_MISSING_TABLES_DDL`) + semeia stage_map. Chamado logo após
    `Base.metadata.create_all(engine)` em `_prepare_test_db`.
    """
    from sqlalchemy import text as _sql

    with engine.begin() as conn:
        for stmt in _MISSING_TABLES_DDL:
            conn.execute(_sql(stmt))
        # Garantia do remarketing (migration 0090): o MESMO DDL da migration,
        # importado dela — tabela de teste não pode divergir da de produção.
        for stmt in _ddl_da_migration("0090_garantia_remarketing"):
            conn.execute(_sql(stmt))
        for kid, tw, label, ordem in _STAGE_MAP_SEED:
            conn.execute(
                _sql(
                    "INSERT INTO stage_map (kommo_status_id, twenty_stage, label_pt, ordem, ativo) "
                    "VALUES (:kid, :tw, :label, :ordem, true) "
                    "ON CONFLICT (twenty_stage) DO NOTHING"
                ),
                {"kid": kid, "tw": tw, "label": label, "ordem": ordem},
            )


@pytest.fixture(scope="session", autouse=True)
def _prepare_test_db():
    """Monta schema + seed no DB de teste (uma vez por sessão).

    Sem early-return: a trava de import-time acima garante que só chegamos aqui
    com `TEST_DATABASE_URL` válida e apontando pra FORA de produção. Os DELETEs
    sem WHERE abaixo (`internos_acme`, `corretoras`) só são seguros por causa
    dessa garantia — não afrouxar a trava sem reescrevê-los.
    """
    # Schema montado direto dos models (create_all) em vez de `alembic upgrade
    # head`: as migrations históricas do projeto NÃO são self-contained (várias
    # assumem o estado incremental de produção — seeds, COMMENT em tabelas já
    # renomeadas — e quebram num DB vazio). Pros testes precisamos do schema
    # atual dos models (create_all) MAIS as tabelas/views/constraints que só
    # existem via migration e nunca ganharam classe ORM — ver
    # `_create_schema_gaps` logo abaixo.
    from sqlalchemy import text

    from app.db.models import Base
    from app.db.session import SessionLocal, engine

    Base.metadata.create_all(engine)
    _create_schema_gaps(engine)

    # Seed IDEMPOTENTE por UPSERT — de propósito, não por `DELETE FROM`.
    #
    # A versão antiga fazia `DELETE FROM corretoras` sem WHERE e só funcionava
    # num banco descartado a cada run. Com um banco de teste PERSISTENTE (o
    # nosso, em :5433), qualquer teste que deixe uma linha filha pendurada
    # (leads_snapshot, coaching_observations, ...) faz o DELETE explodir em
    # ForeignKeyViolation NO SETUP e derruba a suíte inteira — sintoma
    # observado: 95 errors alternando com 95 passed entre runs idênticas.
    #
    # UPSERT resolve na raiz: as 3 corretoras canônicas são convergidas pro
    # estado esperado sem nunca precisar apagá-las, e as FKs seguem válidas.
    with SessionLocal() as s:
        for cid, nome, tel, papel, kommo_uid, instance in _CORRETORAS:
            s.execute(
                text(
                    "INSERT INTO corretoras "
                    "(id, nome, telefone, telefone_normalizado, papel, "
                    " kommo_user_id, evolution_instance, ativo) "
                    "VALUES (:id, :n, :t, :t, :papel, :kommo_uid, :instance, true) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "  nome = EXCLUDED.nome, "
                    "  telefone = EXCLUDED.telefone, "
                    "  telefone_normalizado = EXCLUDED.telefone_normalizado, "
                    "  papel = EXCLUDED.papel, "
                    "  kommo_user_id = EXCLUDED.kommo_user_id, "
                    "  evolution_instance = EXCLUDED.evolution_instance, "
                    "  ativo = true"
                ),
                {
                    "id": cid, "n": nome, "t": tel, "papel": papel,
                    "kommo_uid": kommo_uid, "instance": instance,
                },
            )
        # Corretoras que NÃO são do seed (criadas por algum teste com id livre)
        # são removidas — mas só as sem filhos, pra nunca repetir o problema de
        # FK acima. Se alguma tiver filho, fica: é lixo inerte no DB de teste,
        # não vale derrubar a suíte por ele.
        s.execute(
            text(
                "DELETE FROM corretoras c WHERE c.id <> ALL(:ids) "
                "AND NOT EXISTS (SELECT 1 FROM leads_snapshot l "
                "                WHERE l.corretora_id = c.id)"
            ),
            {"ids": [c[0] for c in _CORRETORAS]},
        )
        # Realinha a sequência do SERIAL pro próximo id livre (evita colisão
        # com os ids 1/2/3 fixados acima em inserts futuros sem id explícito).
        s.execute(text(
            "SELECT setval(pg_get_serial_sequence('corretoras', 'id'), "
            "(SELECT MAX(id) FROM corretoras))"
        ))
        # internos_acme não tem tabela filha — limpar por telefone (e não a
        # tabela inteira) mantém o seed idempotente sem varrer nada além da
        # própria linha que vamos reinserir.
        s.execute(
            text("DELETE FROM internos_acme WHERE telefone_normalizado = :t"),
            {"t": _DONA[1]},
        )
        s.execute(
            text(
                "INSERT INTO internos_acme (nome, telefone_normalizado, papel, ativa) "
                "VALUES (:n, :t, 'dona', true)"
            ),
            {"n": _DONA[0], "t": _DONA[1]},
        )
        s.commit()

    yield


# ── Blindagem de env de negócio, camada 2 (por teste) ────────────────────────
# O pin de import-time acima já fixa as flags ANTES de `Settings()` ser
# construído — é ele que faz o trabalho pesado. Esta fixture é a rede embaixo:
# `settings` é um singleton mutável e a suíte inteira faz
# `patch.object(settings, ...)`. Um teste que mute o objeto e morra no meio
# (exceção dentro do `with`, `monkeypatch.setattr` num escopo mais largo)
# deixa o valor sujo pro teste seguinte — vira falha fantasma que só aparece
# na suíte cheia e some quando o teste roda sozinho.
#
# Restaurar valor-a-valor DEPOIS de cada teste custa ~microssegundos e mata
# essa classe inteira de acoplamento. Só as flags de NEGÓCIO que já causaram
# incidente entram aqui — não é um reset geral de settings (isso quebraria os
# testes que legitimamente configuram token/secret via fixture de escopo maior).
# Pin do mapa de cap por corretora: em produção é {"1":200,"2":200,"3":25}, e se
# vazar pro teste ele sobrepõe o `cadencia_cap_diario` que o teste ajusta — o cap
# "nunca é atingido" e 5 testes ficam vermelhos sem motivo aparente.
try:  # noqa: SIM105 — conftest não pode quebrar por causa disto
    from app.config import settings as _settings_pin

    object.__setattr__(_settings_pin, "cadencia_cap_por_corretora", {})
except Exception:
    pass


# ── Gate do emissor: pasta PRÓPRIA da suíte, nunca a de produção ─────────────
# `app/services/emission_gate.py` guarda a pausa do emissor em
# /var/lib/copiloto-emission-gate (root, 0700). Todo envio decorado com
# `@guarded` passa por lá — inclusive em DRY_RUN, porque a admissão vem antes
# do corte do dry-run. Na suíte isso dava dois resultados, os dois ruins:
#   - como `assistente`: PermissionError em ~30 testes de envio (falha fechada);
#   - como root: o teste abriria o lock e leria o ARQUIVO DE PAUSA DE PRODUÇÃO.
# Aqui o módulo é apontado, em import-time (antes de qualquer teste rodar),
# pra uma pasta temporária desta sessão. O código de produção não muda: o
# caminho segue fixo lá, pra operador e serviço nunca discordarem de onde está
# a pausa. A fixture `_gate_do_emissor_isolado` é a rede embaixo.
import tempfile as _tempfile
from pathlib import Path as _Path

from app.services import emission_gate as _emission_gate

_GATE_PRODUCAO = _Path("/var/lib/copiloto-emission-gate")
_GATE_DE_TESTE = _Path(_tempfile.mkdtemp(prefix="copiloto-gate-teste-"))
_emission_gate.DIRECTORY = _GATE_DE_TESTE


@pytest.fixture(autouse=True)
def _gate_do_emissor_isolado():
    """Garante, a CADA teste, que o gate aponta pra pasta da suíte e está aberto.

    Fail-closed: se algo reapontar o gate pra produção, o teste morre aqui em
    vez de tocar o arquivo de pausa real. Teste que queira exercitar a pausa
    escreve em `emission_gate.DIRECTORY` (já é a pasta temporária) — o estado
    é zerado antes do próximo.
    """
    if _emission_gate.DIRECTORY != _GATE_DE_TESTE:
        _emission_gate.DIRECTORY = _GATE_DE_TESTE
    assert _emission_gate.DIRECTORY.resolve() != _GATE_PRODUCAO, (
        "gate do emissor apontando pra produção dentro da suíte"
    )
    (_GATE_DE_TESTE / "paused").unlink(missing_ok=True)
    yield
    if _emission_gate.DIRECTORY != _GATE_DE_TESTE:
        _emission_gate.DIRECTORY = _GATE_DE_TESTE
    (_GATE_DE_TESTE / "paused").unlink(missing_ok=True)


# ── Categoria de template: a suíte não pergunta pra Meta ─────────────────────
# `cloud_lines.templates_utility/templates_marketing` consultam a Graph com o
# token da linha, lido do env-file dela. `/etc/agencia/review.env` (linha da Pessoa12) é
# legível pelo usuário que roda a suíte — então todo teste de envio que não
# substituía as DUAS funções fazia um GET de verdade na WABA de produção: gasta
# a cota do app (compartilhada com gestor e api-disparos) e deixa o resultado
# do teste na mão da Meta. Foi assim que `test_template_nao_aprovado_ainda_
# rotaciona` ficou vermelho: a Meta reclassificou dois templates pra MARKETING
# e a rotação do teste encolheu de 5 pra 3.
# Aqui a consulta devolve None ("não deu pra saber") — exatamente o que o
# código recebe sem rede — e o cache em disco vai pra pasta da sessão, pra a
# suíte nunca ler nem regravar /var/lib/copiloto/linhas_utility.json.
# Teste que quer uma resposta da Meta substitui `_buscar_categoria_na_meta`
# (ou `templates_*`) por conta própria, como já fazem.
from app.services import cloud_lines as _cloud_lines

_cloud_lines._CAT_ARQUIVO = str(_GATE_DE_TESTE / "linhas_utility.json")
_cloud_lines._cat_ultimo_bom.clear()


@pytest.fixture(autouse=True)
def _categoria_de_template_sem_rede(monkeypatch):
    """Sem GET na Graph e sem cache herdado de outro teste."""
    monkeypatch.setattr(
        _cloud_lines, "_buscar_categoria_na_meta", lambda slug, categoria: None
    )
    for cache in (_cloud_lines._cat_cache, _cloud_lines._cat_mkt_cache,
                  _cloud_lines._cat_ultimo_bom):
        cache.clear()
    yield
    for cache in (_cloud_lines._cat_cache, _cloud_lines._cat_mkt_cache,
                  _cloud_lines._cat_ultimo_bom):
        cache.clear()


# ── Kommo: a suíte não sai pra rede ──────────────────────────────────────────
# Medido em 05/10/2026 com um espião de socket na suíte inteira: 12 testes
# abriam conexão de verdade com o Kommo.
#   - 11 por `KommoClient._get` (o `_stage_map` do painel e o consolidador de
#     métricas). Como `assistente` o subdomínio e o token são fictícios e o Kommo
#     devolve erro; como root seriam o subdomínio e o token DE PRODUÇÃO.
#   - 1 por `painel._kommo_fetch_status_events`, que tem o domínio da conta
#     cravado e lê o token de /opt/acme-kommo/.env — legível por `assistente`. Esse
#     consultava a conta real, com credencial real, a cada rodada.
# Só leitura nos dois casos, mas o resultado do teste ficava na mão do Kommo.
# A guarda fica no transporte do httpx: pedido pra *.kommo.com volta 401 daqui
# mesmo, sem socket. É a resposta que o código já tratava (erro de cliente, sem
# retry). Teste do próprio cliente continua trocando `httpx.Client` por mock e
# nem chega aqui.
import httpx as _httpx

_KOMMO_SEM_REDE = {"detail": "suite de testes: Kommo sem rede (conftest)"}
_transporte_real = _httpx.HTTPTransport.handle_request


def _transporte_sem_kommo(self, request):
    host = (request.url.host or "").lower()
    if host == "kommo.com" or host.endswith(".kommo.com"):
        return _httpx.Response(401, json=_KOMMO_SEM_REDE, request=request)
    return _transporte_real(self, request)


@pytest.fixture(autouse=True)
def _kommo_sem_rede(monkeypatch):
    monkeypatch.setattr(_httpx.HTTPTransport, "handle_request", _transporte_sem_kommo)
    yield


_FLAGS_BLINDADAS: tuple[str, ...] = (
    "lead_intake_ativo",
    "roleta_corretoras",
    "intake_meta_page_id",
    "cadencia_cap_por_corretora",
)


@pytest.fixture(autouse=True)
def _env_negocio_estavel():
    """Restaura as flags de negócio ao valor de projeto após CADA teste."""
    from app.config import settings

    antes = {nome: getattr(settings, nome) for nome in _FLAGS_BLINDADAS}
    yield
    for nome, valor in antes.items():
        if getattr(settings, nome, None) != valor:
            object.__setattr__(settings, nome, valor)


@pytest.fixture(autouse=True)
def _trava_dia_util_desligada(request, monkeypatch):
    """Trava de dia útil (02/10/2026) DESLIGADA no resto da suíte.

    Os testes antigos de cadência, nutrição e mensagem planejada usam relógio
    fixo que avança dias (D0 = sexta 10/07; marco de 30 dias = domingo 09/08) ou
    o relógio real — com a trava ligada eles passariam a falhar só por cair num
    fim de semana, sem nada a ver com o que testam. Quem testa a trava marca
    `@pytest.mark.dia_util` e recebe o comportamento de produção.
    """
    if request.node.get_closest_marker("dia_util"):
        yield
        return
    from app.services import dia_util

    monkeypatch.setattr(dia_util, "trava_ligada", lambda: False)
    yield


@pytest.fixture(autouse=True)
def _qualidade_da_linha_de_teste(monkeypatch):
    """Por padrão, nos testes, a Meta responde `UNKNOWN` pra qualidade da linha.

    Desde 05/10/2026 a rampa NÃO envia sem leitura de qualidade (antes valia o
    primeiro degrau), e a suíte não fala com a Graph — sem esta fixture todo
    teste do carrossel pararia em "não consegui ler a qualidade". `UNKNOWN` é
    leitura boa de número novo: primeiro degrau, 80 por dia, o mesmo teto que
    os testes sempre viram. Quem testa a rampa troca `_qualidade_meta` no
    próprio teste.
    """
    from app.services import rampa

    def _unknown(slug):
        return "UNKNOWN"

    # Quem precisa da função de verdade (o teste de que ela nunca levanta)
    # pega por aqui.
    _unknown.real = rampa._qualidade_meta
    monkeypatch.setattr(rampa, "_qualidade_meta", _unknown)
    yield


@pytest.fixture(autouse=True)
def _dentro_do_horario_do_robo(monkeypatch):
    """A suíte roda a qualquer hora; a autorização final de cada mensagem do
    carrossel recusa fora de 09:00–19:00 de SP. Por padrão o relógio dos
    testes está dentro. Quem testa o guarda troca no próprio teste."""
    from app.services import remarketing_agenda

    monkeypatch.setattr(remarketing_agenda, "dentro_do_horario_do_robo",
                        lambda agora=None: True)
    yield


@pytest.fixture(autouse=True)
def linha_disparadora_de_teste(monkeypatch):
    """Por padrão, nos testes, existe UMA linha marcada como disparo: a `corretor`.

    Desde 05/10/2026 o tick do carrossel pergunta ao api-disparos qual número
    está marcado `disparo` e NÃO envia sem resposta — e a suíte não sai da
    máquina. Sem esta fixture todo teste do carrossel pararia em
    `linha_disparadora_indisponivel`, que é o comportamento certo em produção
    e ruído aqui.

    Quem testa a marca em si (`tests/test_linha_disparadora.py`) redefine esta
    fixture no próprio módulo e troca `_buscar_numeros`.
    """
    from app.services import cloud_lines as cl
    from app.services import linha_disparadora as ld

    linha = cl.line_by_slug("corretor") or cl.CloudLine(
        phone_id="000000000000000", slug="corretor", papel="corretora",
        env_file="", token_var="",
    )
    monkeypatch.setattr(ld, "resolver", lambda: ld.Resolucao(linha=linha))


@pytest.fixture(autouse=True)
def opt_out_api_disparos_de_teste(monkeypatch):
    """Por padrão, nos testes, o api-disparos não tem opt-out de ninguém.

    Desde 07/10/2026 a nutrição lê o opt-out de lá a cada volta e NÃO manda
    marco sem resposta (fail-closed) — e a suíte não abre o banco de produção
    do api-disparos. Devolve a lista de telefones; quem testa a fonte troca
    `_buscar_telefones` no próprio teste (lista ou exceção).
    """
    from app.services import opt_out_api_disparos as oad

    telefones: list[str] = []
    monkeypatch.setattr(oad, "_buscar_telefones", lambda: list(telefones))
    return telefones


# ── IA do lado: a suíte nunca chama o modelo de verdade ──────────────────────
# (05/10/2026) Resumo no card e opinião no que a regra chamou de `indefinida`
# nascem LIGADOS. Aqui o modelo fica "fora do ar" (levanta), que é exatamente o
# caso em que o comportamento tem que ser o de antes — a suíte inteira passando
# assim é a prova. O resumo, que em produção sai numa thread depois da
# transferência, aqui só é anotado em `ia_resumos_agendados` (nada solto em
# thread no meio de outro teste). `tests/test_ia_do_lado.py` troca os dois.
class _ModeloForaDoArNosTestes:
    def complete(self, system, user, json_schema=None):
        raise RuntimeError("suite de testes: modelo fora do ar (conftest)")

    def complete_text(self, system, user):
        raise RuntimeError("suite de testes: modelo fora do ar (conftest)")


@pytest.fixture(autouse=True)
def ia_resumos_agendados(monkeypatch):
    from app.services import ia_do_lado

    agendados: list = []
    monkeypatch.setattr(ia_do_lado, "_provider_factory", _ModeloForaDoArNosTestes)
    monkeypatch.setattr(ia_do_lado, "_em_segundo_plano", agendados.append)
    return agendados
