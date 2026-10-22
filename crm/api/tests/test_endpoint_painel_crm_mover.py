"""POST /painel/crm/mover — arrastar card no kanban do CRM 2.0 (B7).

Cobre:
  - 403 sem header / usuário desconhecido;
  - 400 stage_destino fora dos 8 canônicos / lead_id ausente;
  - 404 lead inexistente;
  - escopo: corretora só move lead DELA (owner_id = id interno OU
    kommo_user_id); admin move qualquer um;
  - move grava leads.stage_id + lead_events tipo='stage_manual_v2'
    (payload origem/destino/quem);
  - destino "Follow-up" liga a cadência (flag CADENCIA_AUTO_ENROLL ligada,
    lead da Pessoa12) — e NÃO liga com a flag desligada (move segue ok);
  - destino "Venda perdida" só move (sem cadência);
  - noop quando o lead já está no stage destino (sem evento manual novo).

DB isolado: SQLite in-memory (mesmo padrão de test_endpoint_painel_crm_board.py)
injetado via monkeypatch de painel.SessionLocal. Zero prod, zero envio —
matrícula na cadência só grava estado; quem toca é o tick (DRY-RUN).
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
    CadenciaEstado,
    CadenciaEvento,
    Corretora,
    Lead,
    LeadEvent,
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

# Mesmos valores canônicos de painel.py (CORRETORA_KOMMO_USER) e do seed do
# conftest — Pessoa12 id=3 / instância acme_corretora_a (cloud_corretora_a.INSTANCE).
CORRETOR_ID = 3
CORRETOR_KOMMO_USER = 10000003
CORRETOR_ID = 1


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
            CadenciaEstado.__table__,
            CadenciaEvento.__table__,
        ],
    )
    f = sessionmaker(bind=engine)
    monkeypatch.setattr(painel, "SessionLocal", f)
    with f() as s:
        st_novo = PipelineStage(tenant_id=1, nome="Novo", ordem=1)
        st_follow = PipelineStage(tenant_id=1, nome="Follow-up", ordem=3)
        s.add_all([st_novo, st_follow])
        s.add(
            Corretora(
                id=CORRETOR_ID, nome="Pessoa3", telefone="+5500000000000",
                kommo_user_id=CORRETOR_KOMMO_USER, evolution_instance="acme_corretora_a",
                ativo=True, papel="corretora_atendedora_de_dona",
            )
        )
        s.flush()
        # lead da Pessoa12 (owner = kommo_user_id, como grava o webhook fino)
        s.add(Lead(id=101, tenant_id=1, external_ref="kommo_101", nome="Lead Pessoa12",
                   telefone="+5500000000000", owner_id=CORRETOR_KOMMO_USER,
                   stage_id=st_novo.id, created_at=AGORA, updated_at=AGORA))
        # lead da Pessoa10 (owner = id interno, como grava o inspector)
        s.add(Lead(id=102, tenant_id=1, external_ref="kommo_102", nome="Lead Pessoa10",
                   telefone="+5500000000000", owner_id=CORRETOR_ID,
                   stage_id=st_novo.id, created_at=AGORA, updated_at=AGORA))
        # lead sem dono
        s.add(Lead(id=103, tenant_id=1, external_ref="kommo_103", nome="Órfão",
                   telefone=None, owner_id=None, stage_id=st_novo.id,
                   created_at=AGORA, updated_at=AGORA))
        s.commit()
    return f


@pytest.fixture()
def client(factory):
    return TestClient(app)


@pytest.fixture()
def flag_ligada():
    with patch.object(settings, "cadencia_auto_enroll", True):
        yield


def _mover(client, lead_id, stage, user="dona"):
    return client.post(
        "/painel/crm/mover",
        json={"lead_id": lead_id, "stage_destino": stage},
        headers={"X-Painel-User": user} if user else {},
    )


def _cadencias(factory) -> list[CadenciaEstado]:
    with factory() as s:
        return s.query(CadenciaEstado).all()


# ── auth ──────────────────────────────────────────────────────────────────────

def test_sem_header_403(client):
    assert _mover(client, 101, "Follow-up", user=None).status_code == 403


def test_usuario_desconhecido_403(client):
    assert _mover(client, 101, "Follow-up", user="hacker").status_code == 403


# ── validação ─────────────────────────────────────────────────────────────────

def test_stage_invalido_400(client):
    r = _mover(client, 101, "Etapa Inventada")
    assert r.status_code == 400
    assert r.json()["detail"] == "stage_invalido"


def test_lead_id_ausente_400(client):
    r = client.post(
        "/painel/crm/mover",
        json={"stage_destino": "Follow-up"},
        headers={"X-Painel-User": "dona"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "lead_id_obrigatorio"


def test_lead_inexistente_404(client):
    assert _mover(client, 999, "Follow-up").status_code == 404


# ── escopo corretora ──────────────────────────────────────────────────────────

def test_corretora_nao_move_lead_de_outra(client, factory):
    r = _mover(client, 102, "Proposta", user="corretor")  # lead da Pessoa10
    assert r.status_code == 403
    assert r.json()["detail"] == "lead_de_outra_corretora"


def test_corretora_nao_move_lead_sem_dono(client):
    assert _mover(client, 103, "Proposta", user="corretor").status_code == 403


def test_corretora_move_lead_dela_por_kommo_user_id(client, factory):
    r = _mover(client, 101, "Proposta", user="corretor")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    with factory() as s:
        lead = s.get(Lead, 101)
        stage = s.get(PipelineStage, lead.stage_id)
        assert stage.nome == "Proposta"


def test_corretor_move_lead_dela_por_id_interno(client, factory):
    assert _mover(client, 102, "Em atendimento", user="corretor").status_code == 200


# ── move + evento stage_manual_v2 ─────────────────────────────────────────────

def test_admin_move_grava_stage_e_evento(client, factory):
    r = _mover(client, 102, "Visita marcada", user="dona")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["stage_destino"] == "Visita marcada"
    assert body["cadencia_ligada"] is False
    with factory() as s:
        lead = s.get(Lead, 102)
        assert s.get(PipelineStage, lead.stage_id).nome == "Visita marcada"
        ev = (
            s.query(LeadEvent)
            .filter(LeadEvent.lead_id == 102, LeadEvent.tipo == "stage_manual_v2")
            .one()
        )
        assert ev.payload["quem"] == "dona"
        assert ev.payload["origem"] == body["de"]
        assert ev.payload["destino"] == body["para"]


def test_venda_perdida_so_move_sem_cadencia(client, factory, flag_ligada):
    r = _mover(client, 101, "Venda perdida", user="dona")
    assert r.status_code == 200
    assert r.json()["cadencia_ligada"] is False
    assert _cadencias(factory) == []


def test_noop_mesmo_stage_nao_gera_evento(client, factory):
    r = _mover(client, 101, "Novo", user="dona")
    assert r.status_code == 200
    assert r.json()["noop"] is True
    with factory() as s:
        assert (
            s.query(LeadEvent)
            .filter(LeadEvent.tipo == "stage_manual_v2")
            .count()
            == 0
        )


# ── Follow-up liga a cadência ─────────────────────────────────────────────────

def test_followup_liga_cadencia(client, factory, flag_ligada):
    r = _mover(client, 101, "Follow-up", user="corretor")
    assert r.status_code == 200
    body = r.json()
    assert body["cadencia_ligada"] is True
    assert body["cadencia_id"] is not None
    rows = _cadencias(factory)
    assert len(rows) == 1
    est = rows[0]
    assert est.telefone == "+5500000000000"
    assert est.corretora_id == CORRETOR_ID
    assert est.lead_id == "kommo_101"
    assert est.status == "ativa"
    assert est.toque_atual == 0


def test_followup_flag_desligada_move_sem_cadencia(client, factory):
    # flag OFF explícita — o .env local pode já ter CADENCIA_AUTO_ENROLL=true
    with patch.object(settings, "cadencia_auto_enroll", False):
        r = _mover(client, 101, "Follow-up", user="corretor")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["cadencia_ligada"] is False
    assert _cadencias(factory) == []
    with factory() as s:
        lead = s.get(Lead, 101)
        assert s.get(PipelineStage, lead.stage_id).nome == "Follow-up"


def test_followup_falha_enroll_nao_desfaz_move(client, factory, flag_ligada):
    with patch(
        "app.services.lead_spine._enroll_cadencia_followup",
        side_effect=RuntimeError("boom"),
    ):
        r = _mover(client, 101, "Follow-up", user="corretor")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["cadencia_ligada"] is False
    with factory() as s:
        lead = s.get(Lead, 101)
        assert s.get(PipelineStage, lead.stage_id).nome == "Follow-up"
