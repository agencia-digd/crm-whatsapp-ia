"""Bloco `remarketing_corretor` do GET /painel/visao-geral.

Contexto (decisão Dona 26/07): Pessoa3 (corretora_id 3, instância Cloud
`acme_corretora_a`) saiu da roleta de leads novos e virou a operação de
REMARKETING. Régua dela: respostas → agendamentos.

Cobre:
  - calcular_remarketing_corretor: contagens (toques template, leads distintos que
    responderam, cadências respondidas, agendamentos via `visitas`), filtro de
    período e taxa resposta→agendamento;
  - escopo no endpoint: admin vê, a própria Pessoa12 vê, outra corretora NÃO vê.

DB isolado: SQLite in-memory (mesmo padrão de test_endpoint_painel_cadencia)
injetado via monkeypatch de painel.SessionLocal. Zero prod, zero envio.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.painel as painel
from app.db.models import Base, CadenciaEstado, MensagemWhatsapp
from app.main import app


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


@compiles(__import__("sqlalchemy.dialects.postgresql", fromlist=["UUID"]).UUID, "sqlite")
def _compile_uuid_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "CHAR(36)"


JUL = lambda d, h=12: datetime(2026, 7, d, h, 0, 0, tzinfo=timezone.utc)  # noqa: E731
DI, DF = date(2026, 7, 1), date(2026, 7, 31)


def _msg(instance: str, tel: str, *, outbound: bool, tipo: str, quando: datetime) -> MensagemWhatsapp:
    return MensagemWhatsapp(
        id=uuid.uuid4(), tenant_id=1,
        evolution_message_id=f"ev-{uuid.uuid4().hex[:8]}", instance=instance,
        remote_jid=f"{tel.lstrip('+')}@s.whatsapp.net",
        contato_telefone=tel, from_corretora=outbound,
        tipo_mensagem=tipo, conteudo_texto="x",
        timestamp_evolution=quando,
    )


@pytest.fixture()
def factory(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine, tables=[MensagemWhatsapp.__table__, CadenciaEstado.__table__]
    )
    fac = sessionmaker(bind=engine)
    # `visitas` não tem model SQLAlchemy (painel consulta via SQL cru) — DDL mínima.
    with fac() as s:
        s.execute(sa_text(
            "CREATE TABLE visitas (id INTEGER PRIMARY KEY, corretora_id INTEGER, "
            "status TEXT, data_hora TIMESTAMP)"
        ))
        s.commit()
    monkeypatch.setattr(painel, "SessionLocal", fac)
    return fac


def _seed(factory) -> None:
    inst = painel.REMARKETING_INSTANCE  # acme_corretora_a
    with factory() as s:
        s.add_all([
            # toques (template outbound da instância) — 3 em julho contam
            _msg(inst, "+5500000000000", outbound=True, tipo="template", quando=JUL(2)),
            _msg(inst, "+5500000000000", outbound=True, tipo="template", quando=JUL(10)),
            _msg(inst, "+5500000000000", outbound=True, tipo="template", quando=JUL(20)),
            # fora do período (junho) — não conta
            _msg(inst, "+5500000000000", outbound=True, tipo="template",
                 quando=datetime(2026, 6, 15, 12, tzinfo=timezone.utc)),
            # outbound não-template — não conta como toque
            _msg(inst, "+5500000000000", outbound=True, tipo="conversation", quando=JUL(3)),
            # template de OUTRA instância — não conta
            _msg("assistente", "+5500000000000", outbound=True, tipo="template", quando=JUL(5)),
            # respostas: fone 1 responde 2x (conta 1), fone 2 responde 1x → 2 distintos
            _msg(inst, "+5500000000000", outbound=False, tipo="conversation", quando=JUL(2, 14)),
            _msg(inst, "+5500000000000", outbound=False, tipo="conversation", quando=JUL(4)),
            _msg(inst, "+5500000000000", outbound=False, tipo="conversation", quando=JUL(11)),
            # inbound em junho — fora do período
            _msg(inst, "+5500000000000", outbound=False, tipo="conversation",
                 quando=datetime(2026, 6, 20, 12, tzinfo=timezone.utc)),
            # inbound noutra instância — não conta
            _msg("assistente", "+5500000000000", outbound=False, tipo="conversation", quando=JUL(12)),
        ])
        s.add_all([
            # 2 cadências respondidas da Pessoa12 em julho — contam
            CadenciaEstado(tenant_id=1, lead_id="k1", telefone="+5500000000000",
                           corretora_id=3, toque_atual=2, status="respondida",
                           criado_em=JUL(1), atualizado_em=JUL(2, 15)),
            CadenciaEstado(tenant_id=1, lead_id="k2", telefone="+5500000000000",
                           corretora_id=3, toque_atual=1, status="respondida",
                           criado_em=JUL(9), atualizado_em=JUL(11)),
            # respondida em junho — fora do período
            CadenciaEstado(tenant_id=1, lead_id="k3", telefone="+5500000000000",
                           corretora_id=3, toque_atual=3, status="respondida",
                           criado_em=datetime(2026, 6, 1, tzinfo=timezone.utc),
                           atualizado_em=datetime(2026, 6, 10, tzinfo=timezone.utc)),
            # ativa (não respondida) — não conta
            CadenciaEstado(tenant_id=1, lead_id="k4", telefone="+5500000000000",
                           corretora_id=3, toque_atual=1, status="ativa",
                           criado_em=JUL(5), atualizado_em=JUL(5)),
        ])
        # visitas (agendamentos — mesma fonte do funil): 2 da Pessoa12 em julho contam
        for cid, status, quando in [
            (3, "agendada", JUL(15)),
            (3, "compareceu", JUL(22)),
            (3, "cancelada", JUL(18)),          # cancelada não conta
            (1, "agendada", JUL(16)),           # outra corretora não conta
            (3, "agendada", datetime(2026, 8, 2, 12, tzinfo=timezone.utc)),  # agosto
        ]:
            s.execute(sa_text(
                "INSERT INTO visitas (corretora_id, status, data_hora) "
                "VALUES (:c, :s, :d)"
            ), {"c": cid, "s": status, "d": quando})
        s.commit()


# ── calcular_remarketing_corretor ──────────────────────────────────────────────────

def test_contagens_e_taxa(factory):
    _seed(factory)
    out = painel.calcular_remarketing_corretor(DI, DF)
    assert out["toques_enviados"] == 3
    assert out["leads_responderam"] == 2
    assert out["respostas_cadencia"] == 2
    assert out["agendamentos"] == 2
    # 2 agendamentos / 2 leads que responderam = 100.0%
    assert out["taxa_resposta_para_agendamento"] == 100.0
    assert out["corretora_id"] == 3
    assert out["instance"] == "acme_corretora_a"


def test_periodo_sem_dados_zera_e_taxa_none(factory):
    _seed(factory)
    out = painel.calcular_remarketing_corretor(date(2026, 5, 1), date(2026, 5, 31))
    assert out["toques_enviados"] == 0
    assert out["leads_responderam"] == 0
    assert out["respostas_cadencia"] == 0
    assert out["agendamentos"] == 0
    assert out["taxa_resposta_para_agendamento"] is None


# ── escopo no endpoint /painel/visao-geral ───────────────────────────────────

_SENTINELA = {
    "corretora_id": 3, "label": "Pessoa12", "instance": "acme_corretora_a",
    "toques_enviados": 662, "leads_responderam": 76, "respostas_cadencia": 51,
    "agendamentos": 1, "taxa_resposta_para_agendamento": 1.3,
}

_FUNIL_CORR = {
    "corretora_id": 3, "label": "Pessoa12", "nome": "Pessoa3", "leads": 0,
    "atendidos": 0, "recuperados": 0, "agendados": 0, "visitas": 0,
    "vendas": 0, "vgv": 0.0, "ticket_medio": None, "conv_atendeu": None,
    "conv_agendou": None, "conv_visitou": None, "conv_vendeu": None,
}

_EQ = {"leads": 0, "atendidos": 0, "agendados": 0, "visitas": 0, "vendas": 0,
       "vgv": 0.0, "ticket_medio": None, "conv_atendeu": None,
       "conv_agendou": None, "conv_visitou": None, "conv_vendeu": None}

_ATENCAO = {
    "data": "2026-07-26",
    "leads_quentes": {"total": 0, "por_corretora": []},
    "leads_mornos_com_gap": {"total": 0, "por_corretora": []},
    "visitas_hoje": [],
    "tarefas_atrasadas": {"total": 0, "por_corretora": []},
}


@pytest.fixture()
def client_endpoint(factory, monkeypatch):
    """TestClient com todos os cálculos pesados stubados — só o gate do
    remarketing_corretor (viewer) fica real."""
    monkeypatch.setattr(painel, "calcular_funil", lambda di, df, **k: {
        "corretoras": [_FUNIL_CORR, {**_FUNIL_CORR, "corretora_id": 1, "label": "Pessoa10", "nome": "Pessoa5"}],
        "equipe": dict(_EQ),
    })
    monkeypatch.setattr(painel, "calcular_metricas_meta_ads", lambda di, df: {
        "spend": 0, "leads_meta": 0, "clicks": 0, "impressions": 0,
        "cpl": None, "cpc": None,
    })
    monkeypatch.setattr(painel, "listar_visitas", lambda di, df: {
        "total": 0, "taxa_comparecimento": None, "items": [], "por_status": {},
    })
    monkeypatch.setattr(painel, "vendas_por_origem", lambda di, df: {
        o: {"vendas": 0, "vgv": 0.0} for o in painel.ORIGENS_VENDA
    })
    monkeypatch.setattr(painel, "detectar_alertas", lambda *a, **k: [])
    monkeypatch.setattr(painel, "calcular_comparativo", lambda *a, **k: None)
    monkeypatch.setattr(painel, "calcular_vazamento_real", lambda di, df: {})
    monkeypatch.setattr(painel, "calcular_recuperados", lambda di, df: {
        "total": 0, "movimentos": 0, "por_user": [],
    })
    monkeypatch.setattr(painel, "calcular_recovery_corretor",
                        lambda di, df: painel._empty_recovery_corretor())
    monkeypatch.setattr(painel, "calcular_atencao_hoje", lambda: dict(_ATENCAO))
    monkeypatch.setattr(painel, "calcular_tendencia_14d", lambda: {
        "dias": [], "meta_leads": [], "mensagens_total": [],
        "mensagens_enviadas": [], "mensagens_recebidas": [],
        "mensagens_por_corretora": {},
    })
    monkeypatch.setattr(painel, "calcular_saude_sistema", lambda: {})
    monkeypatch.setattr(painel, "_frescor_fontes", lambda: {})
    monkeypatch.setattr(painel, "calcular_remarketing_corretor",
                        lambda di, df: dict(_SENTINELA))
    return TestClient(app)


def _get(client, user):
    return client.get("/painel/visao-geral", headers={"X-Painel-User": user})


def test_admin_ve_remarketing_corretor(client_endpoint):
    r = _get(client_endpoint, "dona")
    assert r.status_code == 200
    assert r.json()["remarketing_corretor"] == _SENTINELA


def test_propria_corretor_ve_remarketing_corretor(client_endpoint):
    r = _get(client_endpoint, "corretor")
    assert r.status_code == 200
    assert r.json()["remarketing_corretor"] == _SENTINELA


def test_outra_corretora_nao_ve(client_endpoint):
    r = _get(client_endpoint, "corretor")
    assert r.status_code == 200
    assert r.json()["remarketing_corretor"] is None
