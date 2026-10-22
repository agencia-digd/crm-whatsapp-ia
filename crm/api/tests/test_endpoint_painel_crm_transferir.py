"""POST /painel/crm/transferir — transferência de lead entre corretoras.

Cobre:
  - 403 sem header / usuário desconhecido;
  - 400 corretora_destino ausente/ inválida / lead_id ausente;
  - 404 lead inexistente;
  - escopo: corretora só transfere lead DELA (owner_id = id interno OU
    kommo_user_id); admin transfere qualquer um;
  - transferência grava leads.owner_id + owner_editado_manual + lead_events
    tipo='transferencia' (payload de/para/quem/motivo) + atualiza
    leads_snapshot.corretora_id (com corretora_editado_manual) e
    lead_analyses.corretora_id;
  - noop quando o destino já é a dona atual (sem evento novo);
  - shadow do Kommo NÃO desfaz a transferência (owner_editado_manual).

DB isolado: SQLite in-memory (mesmo padrão de test_endpoint_painel_crm_mover.py)
injetado via monkeypatch de painel.SessionLocal. Zero prod, ZERO WhatsApp —
o endpoint não envia nada (aviso é in-app).
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.painel as painel
from app.config import settings
from app.db.models import (
    Base,
    Corretora,
    Lead,
    LeadAnalysis,
    LeadEvent,
    LeadSnapshot,
    PipelineStage,
)
from app.main import app


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


AGORA = datetime.now(timezone.utc)

# Mesmos valores canônicos de painel.py (CORRETORA_KOMMO_USER).
CORRETOR_ID = 1
CORRETOR_ID = 3
CORRETOR_KOMMO_USER = 10000003


@pytest.fixture()
def factory(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Corretora.__table__,
            PipelineStage.__table__,
            Lead.__table__,
            LeadEvent.__table__,
            LeadSnapshot.__table__,
            LeadAnalysis.__table__,
        ],
    )
    f = sessionmaker(bind=engine)
    monkeypatch.setattr(painel, "SessionLocal", f)
    with f() as s:
        st_novo = PipelineStage(tenant_id=1, nome="Novo", ordem=1)
        s.add(st_novo)
        s.add_all([
            Corretora(id=CORRETOR_ID, nome="Pessoa5", telefone="+5500000000000",
                      kommo_user_id=10000006, evolution_instance="acme_corretora_c",
                      ativo=True, papel="corretora_solo"),
            Corretora(id=CORRETOR_ID, nome="Pessoa3", telefone="+5500000000000",
                      kommo_user_id=CORRETOR_KOMMO_USER, evolution_instance="acme_corretora_a",
                      ativo=True, papel="corretora_atendedora_de_dona"),
        ])
        s.flush()
        # lead da Pessoa12 (owner = kommo_user_id, como grava o webhook fino)
        s.add(Lead(id=101, tenant_id=1, external_ref="9101", nome="Lead Pessoa12",
                   telefone="+5500000000000", owner_id=CORRETOR_KOMMO_USER,
                   stage_id=st_novo.id, created_at=AGORA, updated_at=AGORA))
        # lead da Pessoa10 (owner = id interno, como grava o inspector)
        s.add(Lead(id=102, tenant_id=1, external_ref="9102", nome="Lead Pessoa10",
                   telefone="+5500000000000", owner_id=CORRETOR_ID,
                   stage_id=st_novo.id, created_at=AGORA, updated_at=AGORA))
        # lead sem dono
        s.add(Lead(id=103, tenant_id=1, external_ref="9103", nome="Órfão",
                   telefone=None, owner_id=None, stage_id=st_novo.id,
                   created_at=AGORA, updated_at=AGORA))
        # snapshot + análise do lead da Pessoa12 (escopo da thread/coaching)
        s.add(LeadSnapshot(lead_id="9101", nome="Lead Pessoa12",
                           telefone="+5500000000000", corretora_id=CORRETOR_ID))
        s.add(LeadAnalysis(lead_id="9101", corretora_id=CORRETOR_ID,
                           temperatura="quente", status="atendido",
                           raw_analysis={}))
        s.commit()
    return f


@pytest.fixture()
def client(factory):
    return TestClient(app)


def _transferir(client, lead_id, destino, user="dona", motivo=None):
    body = {"lead_id": lead_id, "corretora_destino": destino}
    if motivo is not None:
        body["motivo"] = motivo
    return client.post(
        "/painel/crm/transferir",
        json=body,
        headers={"X-Painel-User": user} if user else {},
    )


# ── auth ──────────────────────────────────────────────────────────────────────

def test_sem_header_403(client):
    assert _transferir(client, 101, CORRETOR_ID, user=None).status_code == 403


def test_usuario_desconhecido_403(client):
    assert _transferir(client, 101, CORRETOR_ID, user="hacker").status_code == 403


# ── validação ─────────────────────────────────────────────────────────────────

def test_lead_id_ausente_400(client):
    r = client.post(
        "/painel/crm/transferir",
        json={"corretora_destino": CORRETOR_ID},
        headers={"X-Painel-User": "dona"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "lead_id_obrigatorio"


def test_destino_ausente_400(client):
    r = client.post(
        "/painel/crm/transferir",
        json={"lead_id": 101},
        headers={"X-Painel-User": "dona"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "corretora_destino_obrigatoria"


def test_destino_invalido_400(client):
    r = _transferir(client, 101, 99)
    assert r.status_code == 400
    assert r.json()["detail"] == "corretora_invalida"


def test_lead_inexistente_404(client):
    assert _transferir(client, 999, CORRETOR_ID).status_code == 404


# ── escopo corretora ──────────────────────────────────────────────────────────

def test_corretora_nao_transfere_lead_de_outra(client):
    # Pessoa12 tentando transferir lead da Pessoa10 → 403
    r = _transferir(client, 102, CORRETOR_ID, user="corretor")
    assert r.status_code == 403
    assert r.json()["detail"] == "lead_de_outra_corretora"


def test_corretora_nao_transfere_lead_sem_dono(client):
    assert _transferir(client, 103, CORRETOR_ID, user="corretor").status_code == 403


def test_corretora_transfere_lead_proprio(client, factory):
    # Pessoa12 transfere o PRÓPRIO lead (owner = kommo_user_id dela) pra Pessoa10
    r = _transferir(client, 101, CORRETOR_ID, user="corretor", motivo="cliente pediu")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["de"] == CORRETOR_ID
    assert body["para"] == CORRETOR_ID
    assert body["para_label"] == "Pessoa10"
    with factory() as s:
        lead = s.get(Lead, 101)
        assert lead.owner_id == CORRETOR_ID
        assert lead.owner_editado_manual is True
        ev = (
            s.query(LeadEvent)
            .filter(LeadEvent.lead_id == 101, LeadEvent.tipo == "transferencia")
            .one()
        )
        assert ev.payload["de"] == CORRETOR_ID
        assert ev.payload["para"] == CORRETOR_ID
        assert ev.payload["quem"] == "corretor"
        assert ev.payload["motivo"] == "cliente pediu"
        # escopo da thread/coaching acompanha o novo dono
        snap = s.get(LeadSnapshot, "9101")
        assert snap.corretora_id == CORRETOR_ID
        assert snap.corretora_editado_manual is True
        la = s.query(LeadAnalysis).filter(LeadAnalysis.lead_id == "9101").one()
        assert la.corretora_id == CORRETOR_ID


# ── admin ─────────────────────────────────────────────────────────────────────

def test_admin_transfere_qualquer_lead(client, factory):
    r = _transferir(client, 102, CORRETOR_ID, user="dona")
    assert r.status_code == 200
    assert r.json()["de"] == CORRETOR_ID
    assert r.json()["para"] == CORRETOR_ID
    with factory() as s:
        lead = s.get(Lead, 102)
        assert lead.owner_id == CORRETOR_ID
        assert lead.owner_editado_manual is True


def test_admin_transfere_lead_sem_dono(client, factory):
    r = _transferir(client, 103, CORRETOR_ID, user="dona")
    assert r.status_code == 200
    assert r.json()["de"] is None
    with factory() as s:
        assert s.get(Lead, 103).owner_id == CORRETOR_ID


def test_motivo_opcional(client, factory):
    r = _transferir(client, 102, CORRETOR_ID, user="corretor")
    assert r.status_code == 200
    with factory() as s:
        ev = (
            s.query(LeadEvent)
            .filter(LeadEvent.lead_id == 102, LeadEvent.tipo == "transferencia")
            .one()
        )
        assert ev.payload["motivo"] is None
        assert ev.payload["quem"] == "corretor"


# ── noop ──────────────────────────────────────────────────────────────────────

def test_noop_mesma_corretora_nao_gera_evento(client, factory):
    r = _transferir(client, 101, CORRETOR_ID, user="corretor")  # lead já é da Pessoa12
    assert r.status_code == 200
    assert r.json()["noop"] is True
    with factory() as s:
        assert (
            s.query(LeadEvent)
            .filter(LeadEvent.tipo == "transferencia")
            .count()
            == 0
        )
        assert s.get(Lead, 101).owner_editado_manual is False


# ── shadow do Kommo não desfaz a transferência ────────────────────────────────

def test_shadow_kommo_nao_desfaz_transferencia(client, factory):
    from app.services.lead_spine import upsert_lead_shadow

    assert _transferir(client, 101, CORRETOR_ID, user="corretor").status_code == 200
    # sync do Kommo chega com o responsible ANTIGO (Pessoa12) — não pode reverter
    with patch.object(settings, "lead_spine_shadow", True):
        upsert_lead_shadow(
            {"lead_id": "9101", "nome": "Lead Pessoa12", "corretora_id": CORRETOR_ID},
            session_factory=factory,
        )
    with factory() as s:
        lead = s.get(Lead, 101)
        assert lead.owner_id == CORRETOR_ID  # transferência preservada
        assert lead.owner_editado_manual is True
