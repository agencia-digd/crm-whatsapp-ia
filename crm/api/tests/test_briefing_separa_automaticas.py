"""Resumo das 20h: disparo automático não é envio da corretora (09/10/2026).

A Dona perguntou se o copiloto "está mandando os disparos pra mim e pra Pessoa".
Conferindo 08/10: os 638 "msgs enviadas pelo time" tinham 138 templates de
robô (53 dos 70 da Corretor eram carrossel, 41 dos 71 da Pessoa12 eram nutrição/régua), e
o alerta "pipeline morrendo" acusava a Corretor e a Pessoa12 pela taxa de resposta do
disparo. Agora:

  * snapshot (Inspector) e fallback ao vivo contam como envio do time só o
    escrito por gente — nem template, nem `conteudo_metadata.origem`;
  * o robô ganha placar próprio (enviadas, entregues, lidas, falhas,
    responderam), numa linha 🤖 no total e por corretora.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text as sql_text

from app.agents import agente_assistente as ab
from app.db.session import SessionLocal
from app.delivery.dona_formatter import format_msg1_metrics
from app.inspector import calcular_metricas as cm

SP = ZoneInfo("America/Sao_Paulo")
DIA = date(2020, 1, 15)
INST = "teste_sep_robo"
NOME = "Robo Teste"


def _ts(h: int, m: int = 0) -> datetime:
    return datetime(DIA.year, DIA.month, DIA.day, h, m, tzinfo=SP)


# (from_corretora, jid, tipo, metadata, hora)
_MSGS = [
    # A: a corretora escreveu à mão e o lead respondeu → contato do time
    (True, "+5500000000000@s.whatsapp.net", "text", None, 9),
    (False, "+5500000000000@s.whatsapp.net", "text", None, 10),
    # B: carrossel (template com origem) entregue e respondido
    (True, "+5500000000000@s.whatsapp.net", "template",
     {"origem": "carrossel", "delivery_status": "read"}, 9),
    (False, "+5500000000000@s.whatsapp.net", "text", None, 11),
    # C: template sem origem (régua/Reengajar) que falhou
    (True, "+5500000000000@s.whatsapp.net", "template",
     {"delivery_status": "failed"}, 9),
    # D: texto da Pessoa (IA) — não é template, mas tem origem
    (True, "+5500000000000@s.whatsapp.net", "text",
     {"origem": "pessoa", "delivery_status": "delivered"}, 12),
]


# As tabelas de snapshot só existem via migration (0067 + anteriores); o
# create_all da suíte não as cria. DDL copiado do `pg_dump --schema-only` de
# produção em 09/10/2026.
_SNAPSHOT_DDL = (
    """CREATE TABLE IF NOT EXISTS snapshots_corretora (
        data date NOT NULL, instance text NOT NULL, corretora_nome text NOT NULL,
        envios integer DEFAULT 0 NOT NULL, recebidos integer DEFAULT 0 NOT NULL,
        contatos integer DEFAULT 0 NOT NULL, responderam integer DEFAULT 0 NOT NULL,
        taxa_resposta numeric(5,2), status text,
        envios_crm integer DEFAULT 0 NOT NULL, recebidos_crm integer DEFAULT 0 NOT NULL,
        contatos_crm integer DEFAULT 0 NOT NULL, responderam_crm integer DEFAULT 0 NOT NULL,
        taxa_resposta_crm numeric(5,2),
        PRIMARY KEY (data, instance))""",
    """CREATE TABLE IF NOT EXISTS snapshots_diarios (
        data date PRIMARY KEY,
        total_envios integer DEFAULT 0 NOT NULL, total_recebidos integer DEFAULT 0 NOT NULL,
        total_contatos integer DEFAULT 0 NOT NULL, total_msgs integer DEFAULT 0 NOT NULL,
        gerado_em timestamp with time zone DEFAULT now() NOT NULL, inspector_versao text,
        total_envios_crm integer DEFAULT 0 NOT NULL, total_recebidos_crm integer DEFAULT 0 NOT NULL,
        total_contatos_crm integer DEFAULT 0 NOT NULL)""",
)


@pytest.fixture
def mensagens():
    ids: list[str] = []
    with SessionLocal() as s:
        for stmt in _SNAPSHOT_DDL:
            s.execute(sql_text(stmt))
        for fc, jid, tipo, meta, h in _MSGS:
            mid = f"TEST-sep-{uuid.uuid4()}"
            ids.append(mid)
            s.execute(
                sql_text(
                    "INSERT INTO mensagens_whatsapp (evolution_message_id, instance, "
                    " remote_jid, contato_telefone, from_corretora, tipo_mensagem, "
                    " conteudo_texto, conteudo_metadata, timestamp_evolution) "
                    "VALUES (:mid, :inst, :jid, :tel, :fc, :tipo, '', "
                    " CAST(:meta AS jsonb), :ts)"
                ),
                {"mid": mid, "inst": INST, "jid": jid, "tel": jid.split("@")[0],
                 "fc": fc, "tipo": tipo,
                 "meta": json.dumps(meta) if meta else None, "ts": _ts(h)},
            )
        s.commit()
    yield ids
    with SessionLocal() as s:
        s.execute(sql_text("DELETE FROM mensagens_whatsapp WHERE evolution_message_id = ANY(:i)"),
                  {"i": ids})
        s.execute(sql_text("DELETE FROM snapshots_corretora WHERE data = :d"), {"d": DIA})
        s.execute(sql_text("DELETE FROM snapshots_diarios WHERE data = :d"), {"d": DIA})
        s.commit()


def _corretora() -> dict:
    return {"nome": NOME, "telefone": None, "evolution_instance": INST,
            "evolution_connected_since": None, "canal": "cloud"}


def test_snapshot_conta_so_o_escrito_por_gente(mensagens, monkeypatch):
    monkeypatch.setattr(cm, "_get_instance_nome_map", lambda: {INST: NOME})
    cm.calcular_metricas_dia(DIA)
    with SessionLocal() as s:
        r = s.execute(
            sql_text("SELECT envios, recebidos, contatos, responderam "
                     "FROM snapshots_corretora WHERE data = :d AND instance = :i"),
            {"d": DIA, "i": INST},
        ).one()
    # Antes: envios=4 (1 à mão + 3 robô), contatos=4, responderam=2.
    assert (r.envios, r.recebidos, r.contatos, r.responderam) == (1, 1, 1, 1)


def test_fallback_ao_vivo_usa_a_mesma_regra(mensagens):
    meia_noite = datetime(DIA.year, DIA.month, DIA.day, tzinfo=SP).astimezone(timezone.utc)
    out = ab.fetch_whatsapp_metrics([_corretora()], _cutoff_override=meia_noite)
    m = out[NOME]
    assert (m["msgs_enviadas"], m["contatos_unicos"], m["leads_responderam"]) == (1, 1, 1)


def test_placar_do_robo(mensagens):
    agora = _ts(23).astimezone(timezone.utc)
    d = ab.fetch_disparos_do_dia([_corretora()], _agora=agora)[NOME]
    assert d == {"enviados": 3, "entregues": 2, "lidos": 1, "falhas": 1, "responderam": 1}


def test_placar_do_robo_respeita_o_corte_do_dia(mensagens):
    # rodando no dia seguinte, nada do dia 15 entra
    agora = (_ts(23) + timedelta(days=1)).astimezone(timezone.utc)
    assert ab.fetch_disparos_do_dia([_corretora()], _agora=agora) == {}


def _ctx(disparos):
    wm = {NOME: {"msgs_enviadas": 30, "msgs_recebidas": 24, "contatos_unicos": 7,
                 "leads_responderam": 3, "taxa_resposta_pct": 43,
                 "msgs_enviadas_crm": 18, "contatos_crm": 3,
                 "is_offline": False, "instance_state": "open"}}
    ctx = ab.build_dona_context([], whatsapp_metrics=wm)
    ctx["data"] = "08/10/2026"
    if disparos is not None:
        ctx["disparos"] = disparos
    return ctx


def test_mensagem_mostra_robo_separado_do_time():
    txt = format_msg1_metrics(_ctx({NOME: {"enviados": 41, "entregues": 34, "lidos": 3,
                                           "falhas": 1, "responderam": 7}}))
    linhas = txt.splitlines()
    i = next(n for n, l in enumerate(linhas) if l.startswith("📤 30 msgs enviadas pelo time"))
    assert linhas[i + 1] == ("🤖 41 automática(s) · 34 entregue(s) · 3 lida(s) · "
                             "1 falha(s) · 💬 7 responderam")
    assert "  └ 🤖 41 automática(s) · 34 entregue(s) · 3 lida(s) · 1 falha(s) · " \
           "💬 7 responderam" in linhas


def test_sem_disparo_a_mensagem_fica_como_era():
    txt = format_msg1_metrics(_ctx(None))
    assert "🤖" not in txt
    assert "📤 30 msgs enviadas pelo time · 18 no CRM" in txt
