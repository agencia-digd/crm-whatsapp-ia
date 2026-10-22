"""GET /painel/crm/board — Kanban do CRM 2.0 lendo a espinha canônica.

Cobre:
  - 403 sem header / usuário desconhecido;
  - colunas na ordem de pipeline_stages + cards (nome/telefone/valor/origem/corretora);
  - escopo: corretora vê só os leads dela (e ?corretora= é ignorado pra ela);
  - admin vê tudo e filtra com ?corretora=<id>;
  - lead sem stage cai em "Sem etapa";
  - pipeline_stages vazia → colunas derivadas de leads.stage_id (stages_derivados);
  - card traz tag (espelho da origem), created_at e dias_parado
    (hoje - max(último evento, updated_at); fallback created_at);
  - tudo vazio → vazio=True (estado "aguardando primeira sincronização").

DB isolado: SQLite in-memory (mesmo padrão de test_endpoint_painel_cadencia.py)
injetado via monkeypatch de painel.SessionLocal. Zero prod, zero envio.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.painel as painel
from app.config import settings
from app.db.models import Base, Lead, LeadEvent, PipelineStage
from app.main import app


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


AGORA = datetime.now(timezone.utc)


def _make_client(monkeypatch, seed_fn, crm_ativas: list[int] | None = None):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[PipelineStage.__table__, Lead.__table__, LeadEvent.__table__],
    )
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(painel, "SessionLocal", factory)
    # trava do piloto (item 2): default destes testes = SEM trava ([]) pra
    # cobrir o board em si; o comportamento da trava tem testes próprios.
    monkeypatch.setattr(settings, "crm_corretoras_ativas", crm_ativas or [])
    seed_fn(factory)
    return TestClient(app)


def _seed_completo(factory) -> None:
    with factory() as s:
        st1 = PipelineStage(tenant_id=1, nome="Entrada", ordem=1, tipo="aberta")
        st2 = PipelineStage(tenant_id=1, nome="Em atendimento", ordem=2, tipo="aberta")
        st_outro_tenant = PipelineStage(tenant_id=2, nome="Alheia", ordem=1, tipo="aberta")
        s.add_all([st1, st2, st_outro_tenant])
        s.flush()
        l1 = Lead(tenant_id=1, external_ref="kommo_1", nome="João Silva",
                  telefone="+5500000000000", owner_id=1, stage_id=st1.id,
                  origem="Meta Ads", valor=Decimal("350000.00"),
                  created_at=AGORA - timedelta(days=2), updated_at=AGORA)
        l2 = Lead(tenant_id=1, external_ref="kommo_2", nome="Pessoa Souza",
                  telefone="+5500000000000", owner_id=3, stage_id=st2.id,
                  origem="Indicação", valor=None,
                  created_at=AGORA - timedelta(days=1), updated_at=AGORA)
        l3 = Lead(tenant_id=1, external_ref="kommo_3", nome="",
                  telefone="+5500000000000", owner_id=1, stage_id=None,
                  origem=None, valor=Decimal("120000.50"),
                  created_at=AGORA, updated_at=None)
        # lead de OUTRO tenant — nunca aparece
        l4 = Lead(tenant_id=2, external_ref="x_9", nome="Fantasma",
                  telefone="+5500000000000", owner_id=1, stage_id=st_outro_tenant.id)
        s.add_all([l1, l2, l3, l4])
        s.flush()
        s.add_all([
            LeadEvent(tenant_id=1, lead_id=l1.id, tipo="stage_change",
                      payload={"para": "Entrada"}, at=AGORA - timedelta(hours=5)),
            LeadEvent(tenant_id=1, lead_id=l1.id, tipo="msg_inbound",
                      payload={}, at=AGORA - timedelta(hours=1)),
        ])
        s.commit()


@pytest.fixture()
def client(monkeypatch):
    return _make_client(monkeypatch, _seed_completo)


# ── auth ──────────────────────────────────────────────────────────────────────

def test_sem_header_403(client):
    r = client.get("/painel/crm/board")
    assert r.status_code == 403


def test_usuario_desconhecido_403(client):
    r = client.get("/painel/crm/board", headers={"X-Painel-User": "hacker"})
    assert r.status_code == 403


# ── admin ─────────────────────────────────────────────────────────────────────

def test_admin_ve_tudo_na_ordem_das_stages(client):
    r = client.get("/painel/crm/board", headers={"X-Painel-User": "dona"})
    assert r.status_code == 200
    d = r.json()
    assert d["viewer"]["role"] == "admin"
    assert d["total"] == 3  # só tenant 1
    assert d["stages_derivados"] is False
    assert d["vazio"] is False
    nomes = [c["nome"] for c in d["colunas"]]
    assert nomes == ["Entrada", "Em atendimento", "Sem etapa"]

    entrada = d["colunas"][0]
    assert entrada["total"] == 1
    card = entrada["cards"][0]
    assert card["nome"] == "João Silva"
    assert card["telefone"] == "+5500000000000"
    assert card["valor"] == 350000.0
    assert card["origem"] == "Meta Ads"
    assert card["corretora_label"] == "Pessoa10"
    assert card["ultimo_evento_at"] is not None  # veio de lead_events (max at)
    assert card["tag"] == "Meta Ads"          # tag espelha origem por ora
    assert card["created_at"] is not None     # data de entrada
    assert card["dias_parado"] == 0           # updated_at=AGORA → sem parada

    sem_etapa = d["colunas"][2]
    assert sem_etapa["stage_id"] is None
    assert sem_etapa["cards"][0]["nome"] == "Sem nome"
    assert sem_etapa["cards"][0]["valor"] == 120000.5


def test_admin_filtra_por_corretora(client):
    r = client.get("/painel/crm/board?corretora=3",
                   headers={"X-Painel-User": "dona"})
    d = r.json()
    assert d["total"] == 1
    assert d["filtro_corretora"] == 3
    todos = [c for col in d["colunas"] for c in col["cards"]]
    assert [c["nome"] for c in todos] == ["Pessoa Souza"]
    # colunas continuam TODAS (kanban mantém a régua), só esvaziam
    assert [c["nome"] for c in d["colunas"]] == ["Entrada", "Em atendimento"]


# ── escopo corretora ──────────────────────────────────────────────────────────

def test_corretora_ve_so_os_dela(client):
    r = client.get("/painel/crm/board", headers={"X-Painel-User": "corretor"})
    assert r.status_code == 200
    d = r.json()
    assert d["filtro_corretora"] == 1
    todos = [c for col in d["colunas"] for c in col["cards"]]
    assert {c["corretora_id"] for c in todos} == {1}
    assert d["total"] == 2


def test_corretora_nao_burla_filtro(client):
    # ?corretora=3 é ignorado pra corretora — privacidade entre corretoras
    r = client.get("/painel/crm/board?corretora=3",
                   headers={"X-Painel-User": "corretor"})
    d = r.json()
    assert d["filtro_corretora"] == 1
    todos = [c for col in d["colunas"] for c in col["cards"]]
    assert {c["corretora_id"] for c in todos} == {1}


# ── estados degradados ────────────────────────────────────────────────────────

def _seed_sem_stages(factory) -> None:
    """Espinha meio-populada: leads existem, pipeline_stages do tenant vazia."""
    with factory() as s:
        st_alheio = PipelineStage(tenant_id=2, nome="Alheia", ordem=1)
        s.add(st_alheio)
        s.flush()
        s.add(Lead(tenant_id=1, external_ref="kommo_7", nome="Órfão",
                   telefone="+5500000000000", owner_id=2, stage_id=st_alheio.id,
                   origem="Meta Ads", valor=None, created_at=AGORA))
        s.commit()


def test_stages_vazias_deriva_colunas(monkeypatch):
    c = _make_client(monkeypatch, _seed_sem_stages)
    r = c.get("/painel/crm/board", headers={"X-Painel-User": "dona"})
    d = r.json()
    assert d["stages_derivados"] is True
    assert d["vazio"] is False
    assert len(d["colunas"]) == 1
    assert d["colunas"][0]["nome"].startswith("Etapa ")
    assert d["colunas"][0]["cards"][0]["nome"] == "Órfão"


def test_tudo_vazio_aguardando_sync(monkeypatch):
    c = _make_client(monkeypatch, lambda factory: None)
    r = c.get("/painel/crm/board", headers={"X-Painel-User": "dona"})
    d = r.json()
    assert d["vazio"] is True
    assert d["total"] == 0
    assert d["colunas"] == []


# ── card: tag / dias_parado / data de entrada ────────────────────────────────

def _seed_lead_parado(factory) -> None:
    """Lead com última atividade há 12 dias (updated_at > último evento)."""
    with factory() as s:
        st = PipelineStage(tenant_id=1, nome="Entrada", ordem=1, tipo="aberta")
        s.add(st)
        s.flush()
        lead = Lead(tenant_id=1, external_ref="kommo_9", nome="Parado",
                    telefone="+5500000000000", owner_id=1, stage_id=st.id,
                    origem="Site", valor=None,
                    created_at=AGORA - timedelta(days=40),
                    updated_at=AGORA - timedelta(days=12))
        s.add(lead)
        s.flush()
        s.add(LeadEvent(tenant_id=1, lead_id=lead.id, tipo="msg_inbound",
                        payload={}, at=AGORA - timedelta(days=20)))
        s.commit()


def test_dias_parado_usa_max_de_evento_e_updated_at(monkeypatch):
    c = _make_client(monkeypatch, _seed_lead_parado)
    r = c.get("/painel/crm/board", headers={"X-Painel-User": "dona"})
    card = r.json()["colunas"][0]["cards"][0]
    # max(evento D-20, updated_at D-12) = D-12
    assert card["dias_parado"] == 12
    assert card["tag"] == "Site"


def _seed_lead_sem_atividade(factory) -> None:
    """Lead sem evento e sem updated_at → dias_parado cai no created_at."""
    with factory() as s:
        st = PipelineStage(tenant_id=1, nome="Entrada", ordem=1, tipo="aberta")
        s.add(st)
        s.flush()
        s.add(Lead(tenant_id=1, external_ref="kommo_10", nome="Esquecido",
                   telefone="+5500000000000", owner_id=1, stage_id=st.id,
                   origem="Indicação", valor=None,
                   created_at=AGORA - timedelta(days=35), updated_at=None))
        s.commit()


def test_dias_parado_fallback_created_at(monkeypatch):
    c = _make_client(monkeypatch, _seed_lead_sem_atividade)
    r = c.get("/painel/crm/board", headers={"X-Painel-User": "dona"})
    card = r.json()["colunas"][0]["cards"][0]
    assert card["dias_parado"] == 35


# ── item 2 blueprint: trava "só corretoras com CRM ativo" (default só Pessoa12) ─────

def test_trava_so_corretor_admin_ve_so_leads_da_corretor(monkeypatch):
    """Com a trava default ([3] = Pessoa12), leads de Pessoa10/Pessoa11 NÃO aparecem no
    board — a sombra segue gravando todas, é só exibição."""
    c = _make_client(monkeypatch, _seed_completo, crm_ativas=[3])
    r = c.get("/painel/crm/board", headers={"X-Painel-User": "dona"})
    assert r.status_code == 200
    d = r.json()
    todos = [card for col in d["colunas"] for card in col["cards"]]
    assert [card["nome"] for card in todos] == ["Pessoa Souza"]  # owner_id=3
    assert {card["corretora_id"] for card in todos} == {3}


def test_trava_so_corretor_corretora_sem_crm_ve_vazio(monkeypatch):
    c = _make_client(monkeypatch, _seed_completo, crm_ativas=[3])
    r = c.get("/painel/crm/board", headers={"X-Painel-User": "corretor"})
    assert r.status_code == 200
    d = r.json()
    todos = [card for col in d["colunas"] for card in col["cards"]]
    assert todos == []  # Pessoa10 (id=1) fora da lista de CRM ativo


def test_default_da_trava_e_as_cinco():
    """Config default = [1, 2, 3, 7, 8] — entram Pessoa7 (7) e Corretor (8), decisão da
    Dona em 01/10/2026 (era [1, 2, 3] desde 22/07, commit 70a02b5e, e [3] no
    piloto só-Pessoa12). Se mudar, é decisão consciente, não acidente."""
    from app.config import Settings

    assert Settings.model_fields["crm_corretoras_ativas"].default == [1, 2, 3, 7, 8]


# ── item 3 blueprint: coluna órfã dobra na canônica ───────────────────────────

def _seed_com_orfa(factory) -> None:
    """Funil canônico parcial + coluna órfã "10000001" (id cru do Kommo,
    criada pela sombra antiga) com 1 lead dentro."""
    with factory() as s:
        st_novo = PipelineStage(tenant_id=1, nome="Novo", ordem=1, tipo="aberta")
        st_fup = PipelineStage(tenant_id=1, nome="Follow-up", ordem=3, tipo="aberta")
        st_orfa = PipelineStage(tenant_id=1, nome="10000001", ordem=None, tipo=None)
        s.add_all([st_novo, st_fup, st_orfa])
        s.flush()
        s.add_all([
            Lead(tenant_id=1, external_ref="k1", nome="No Novo",
                 telefone="+5500000000000", owner_id=3, stage_id=st_novo.id,
                 created_at=AGORA, updated_at=AGORA),
            Lead(tenant_id=1, external_ref="k2", nome="Na Orfa",
                 telefone="+5500000000000", owner_id=3, stage_id=st_orfa.id,
                 created_at=AGORA, updated_at=AGORA),
        ])
        s.commit()


def test_coluna_orfa_dobra_na_canonica(monkeypatch):
    c = _make_client(monkeypatch, _seed_com_orfa)
    r = c.get("/painel/crm/board", headers={"X-Painel-User": "dona"})
    assert r.status_code == 200
    d = r.json()
    nomes = [col["nome"] for col in d["colunas"]]
    assert "10000001" not in nomes  # órfã sumiu do kanban
    fup = next(col for col in d["colunas"] if col["nome"] == "Follow-up")
    assert [card["nome"] for card in fup["cards"]] == ["Na Orfa"]  # card migrou
    novo = next(col for col in d["colunas"] if col["nome"] == "Novo")
    assert [card["nome"] for card in novo["cards"]] == ["No Novo"]
