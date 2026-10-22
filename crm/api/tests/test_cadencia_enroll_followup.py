"""Testes do ENROLL-ON-FOLLOWUP da cadência invisível.

Regra (Dona, 10/07): lead movido pra etapa "Follow-up" no CRM → entra AUTOMÁTICO
na cadência. Sem módulo próprio — o gatilho é a etapa. Dois caminhos:

  A) webhook Kommo "Lead stage changed" (leads[status]) → status_id 10000001
     → _auto_enroll_cadencia_followup_corretor (delega pro auto-enroll da Pessoa12);
  B) lead_spine.upsert_lead_shadow detecta stage_change → "Follow-up"
     (caminho do Inspector, que passa etapa_nome real) → _enroll_cadencia_followup.

Cobre: flag desligada, etapa errada, outra corretora, matrícula ok (2 caminhos),
idempotência e best-effort (falha não quebra o upsert nem o webhook).

DB isolado: SQLite in-memory (mesmo padrão de test_cadencia_auto_enroll).
Zero prod, zero envio — matrícula só grava estado; quem toca é o tick (DRY-RUN).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.api.cloud_corretora_a import KOMMO_STAGE_FOLLOWUP
from app.api.kommo_webhook import _auto_enroll_cadencia_followup_corretor
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
from app.services.lead_spine import upsert_lead_shadow


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


# Mesmos valores canônicos do seed de tests/conftest.py / painel.py
CORRETOR_ID = 3
CORRETOR_KOMMO_USER = 10000003
CORRETOR_ID = 1
CORRETOR_KOMMO_USER = 10000006


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            Corretora.__table__,
            CadenciaEstado.__table__,
            CadenciaEvento.__table__,
            Lead.__table__,
            LeadEvent.__table__,
            PipelineStage.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as s:
        s.add_all(
            [
                Corretora(
                    id=CORRETOR_ID, nome="Pessoa5", telefone="+5500000000000",
                    kommo_user_id=CORRETOR_KOMMO_USER, evolution_instance="acme_corretora_c",
                    ativo=True,
                ),
                Corretora(
                    id=CORRETOR_ID, nome="Pessoa3", telefone="+5500000000000",
                    kommo_user_id=CORRETOR_KOMMO_USER, evolution_instance="acme_corretora_a",
                    ativo=True, papel="corretora_atendedora_de_dona",
                ),
            ]
        )
        s.commit()
    return factory


@pytest.fixture()
def flag_ligada():
    with patch.object(settings, "cadencia_auto_enroll", True):
        yield


def _cadencias(session_factory) -> list[CadenciaEstado]:
    with session_factory() as s:
        return s.query(CadenciaEstado).all()


# ── Caminho A: webhook leads[status] ──────────────────────────────────────────

def _status_lead(status_id: int = KOMMO_STAGE_FOLLOWUP, **over):
    """Payload de lead como chega no bloco leads[status][0] do webhook Kommo."""
    lead: dict = {
        "id": "kommo_701",
        "name": "Lead Followup",
        "status_id": status_id,
        "old_status_id": 10,
        "responsible_user_id": CORRETOR_KOMMO_USER,
        "custom_fields": [
            {"id": "101", "name": "Telefone", "code": "PHONE",
             "values": [{"value": "+5500000000000"}]},
        ],
    }
    lead.update(over)
    return lead


def test_webhook_flag_desligada_nao_matricula(session_factory):
    # flag OFF explícita — o default do settings é False, mas o .env de dev/prod pode ligar CADENCIA_AUTO_ENROLL; o teste não pode depender do ambiente
    with patch.object(settings, "cadencia_auto_enroll", False):
        cid = _auto_enroll_cadencia_followup_corretor(_status_lead(), session_factory=session_factory)
    assert cid is None
    assert _cadencias(session_factory) == []


def test_webhook_movido_pra_followup_matricula(session_factory, flag_ligada):
    cid = _auto_enroll_cadencia_followup_corretor(_status_lead(), session_factory=session_factory)
    assert cid is not None
    rows = _cadencias(session_factory)
    assert len(rows) == 1
    est = rows[0]
    assert est.telefone == "+5500000000000"
    assert est.corretora_id == CORRETOR_ID
    assert est.lead_id == "kommo_701"
    assert est.status == "ativa"
    assert est.toque_atual == 0


def test_webhook_ignora_outra_etapa(session_factory, flag_ligada):
    cid = _auto_enroll_cadencia_followup_corretor(
        _status_lead(status_id=10000005), session_factory=session_factory  # Em atendimento
    )
    assert cid is None
    assert _cadencias(session_factory) == []


def test_webhook_ignora_status_id_ausente(session_factory, flag_ligada):
    assert _auto_enroll_cadencia_followup_corretor(
        _status_lead(status_id=None), session_factory=session_factory
    ) is None
    assert _cadencias(session_factory) == []


def test_webhook_ignora_outra_corretora(session_factory, flag_ligada):
    cid = _auto_enroll_cadencia_followup_corretor(
        _status_lead(responsible_user_id=CORRETOR_KOMMO_USER), session_factory=session_factory
    )
    assert cid is None
    assert _cadencias(session_factory) == []


def test_webhook_endpoint_processa_leads_status(flag_ligada):
    """POST no webhook com bloco leads[status] → chega no auto-enroll da Pessoa12
    (fluxo integrado via HTTP; a matrícula em si é coberta nos testes acima —
    SQLite in-memory não cruza a thread do TestClient).
    """
    from unittest.mock import MagicMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.kommo_webhook import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    payload = {"leads": {"status": [_status_lead()]}}

    async def _noop(_):  # noqa: ANN001, ANN202
        return True

    enroll = MagicMock(return_value=1)
    with patch.object(settings, "kommo_webhook_secret", "s3cr3t"), \
         patch("app.api.kommo_webhook.announce", side_effect=_noop), \
         patch("app.api.kommo_webhook._auto_enroll_cadencia_corretor", enroll):
        resp = client.post("/webhook/kommo/lead-created/s3cr3t", json=payload)

    assert resp.status_code == 200
    enroll.assert_called_once()
    assert enroll.call_args.args[0]["id"] == "kommo_701"
    assert int(enroll.call_args.args[0]["status_id"]) == KOMMO_STAGE_FOLLOWUP


def test_webhook_falha_enroll_nao_derruba(flag_ligada):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.kommo_webhook import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    payload = {"leads": {"status": [_status_lead()]}}

    async def _noop(_):  # noqa: ANN001, ANN202
        return True

    with patch.object(settings, "kommo_webhook_secret", "s3cr3t"), \
         patch("app.api.kommo_webhook._auto_enroll_cadencia_followup_corretor",
               side_effect=RuntimeError("boom")), \
         patch("app.api.kommo_webhook.announce", side_effect=_noop):
        resp = client.post("/webhook/kommo/lead-created/s3cr3t", json=payload)

    assert resp.status_code == 200


# ── Caminho B: lead_spine stage_change → Follow-up (Inspector) ────────────────

def _lead_spine(**over):
    base = {
        "lead_id": "kommo_702",
        "nome": "Lead Spine",
        "telefone": "+5500000000000",
        "corretora_id": CORRETOR_ID,
        "etapa_nome": "Primeiro contato",
        "origem": "meta_ads",
    }
    base.update(over)
    return base


def test_spine_stage_change_pra_followup_matricula(session_factory, flag_ligada):
    upsert_lead_shadow(_lead_spine(), session_factory=session_factory)
    assert _cadencias(session_factory) == []  # criação em outra etapa: nada

    upsert_lead_shadow(_lead_spine(etapa_nome="Follow-up"), session_factory=session_factory)
    rows = _cadencias(session_factory)
    assert len(rows) == 1
    est = rows[0]
    assert est.telefone == "+5500000000000"
    assert est.corretora_id == CORRETOR_ID
    assert est.lead_id == "kommo_702"
    assert est.status == "ativa"


def test_spine_flag_desligada_nao_matricula(session_factory):
    # flag OFF explícita — o default do settings é False, mas o .env de dev/prod pode ligar CADENCIA_AUTO_ENROLL; o teste não pode depender do ambiente
    with patch.object(settings, "cadencia_auto_enroll", False):
        upsert_lead_shadow(_lead_spine(), session_factory=session_factory)
        upsert_lead_shadow(_lead_spine(etapa_nome="Follow-up"), session_factory=session_factory)
    assert _cadencias(session_factory) == []


def test_spine_ignora_outra_corretora(session_factory, flag_ligada):
    upsert_lead_shadow(_lead_spine(corretora_id=CORRETOR_ID), session_factory=session_factory)
    upsert_lead_shadow(
        _lead_spine(corretora_id=CORRETOR_ID, etapa_nome="Follow-up"),
        session_factory=session_factory,
    )
    assert _cadencias(session_factory) == []


def test_spine_aceita_owner_kommo_user_id(session_factory, flag_ligada):
    """Webhook fino grava owner_id = kommo_user_id — o enroll aceita os dois."""
    upsert_lead_shadow(
        _lead_spine(corretora_id=None, owner_id=CORRETOR_KOMMO_USER),
        session_factory=session_factory,
    )
    upsert_lead_shadow(
        _lead_spine(corretora_id=None, owner_id=CORRETOR_KOMMO_USER, etapa_nome="Follow-up"),
        session_factory=session_factory,
    )
    rows = _cadencias(session_factory)
    assert len(rows) == 1
    assert rows[0].corretora_id == CORRETOR_ID  # normaliza pro id interno


def test_spine_stage_change_por_status_id_matricula(session_factory, flag_ligada):
    """Sem etapa_nome (webhook fino), o status_id 10000001 identifica o Follow-up."""
    upsert_lead_shadow(
        _lead_spine(etapa_nome=None, etapa_id=10), session_factory=session_factory
    )
    upsert_lead_shadow(
        _lead_spine(etapa_nome=None, etapa_id=10000001), session_factory=session_factory
    )
    assert len(_cadencias(session_factory)) == 1


def test_spine_idempotente(session_factory, flag_ligada):
    upsert_lead_shadow(_lead_spine(), session_factory=session_factory)
    upsert_lead_shadow(_lead_spine(etapa_nome="Follow-up"), session_factory=session_factory)
    # sai e volta pro Follow-up → NÃO duplica (UNIQUE tenant+telefone por baixo)
    upsert_lead_shadow(_lead_spine(etapa_nome="Em atendimento"), session_factory=session_factory)
    upsert_lead_shadow(_lead_spine(etapa_nome="Follow-up"), session_factory=session_factory)
    assert len(_cadencias(session_factory)) == 1


def test_spine_falha_enroll_nao_quebra_upsert(session_factory, flag_ligada):
    """Enroll explodindo NÃO desfaz o stage_change (best-effort pós-commit)."""
    upsert_lead_shadow(_lead_spine(), session_factory=session_factory)
    with patch(
        "app.services.lead_spine._enroll_cadencia_followup",
        side_effect=RuntimeError("boom"),
    ):
        lid = upsert_lead_shadow(
            _lead_spine(etapa_nome="Follow-up"), session_factory=session_factory
        )
    assert lid is not None
    with session_factory() as s:
        lead = s.query(Lead).filter(Lead.external_ref == "kommo_702").one()
        followup = (
            s.query(PipelineStage).filter(PipelineStage.nome == "Follow-up").one()
        )
        assert lead.stage_id == followup.id  # stage_change persistiu
    assert _cadencias(session_factory) == []  # matrícula não aconteceu
