"""Anticonflito B7 — move MANUAL no kanban V2 vence o espelho do Kommo.

Regra: se o lead tem `lead_events` tipo='stage_manual_v2' dentro da janela de
proteção (72h), `upsert_lead_shadow` NÃO sobrescreve `leads.stage_id` com o
dado do Kommo (Inspector/webhook) — loga `lead_spine_stage_manual_preservado`
e mantém o manual. Evento manual mais VELHO que 72h → a sombra volta a mandar.

Sem isso o robô da manhã desfaz o arrasto da Dona. NUNCA escreve de volta no
Kommo (histórico de incidente de loop de PATCH).

Cobre:
  - manual recente (agora) → Kommo não sobrescreve + log de preservação
    + NÃO gera stage_change fantasma;
  - manual velho (>72h) → Kommo sobrescreve normal (stage_change registrado);
  - sem evento manual → comportamento original intacto;
  - preservação não bloqueia update dos demais campos (nome/valor/telefone);
  - upsert preservado não dispara enroll-on-followup (stage não mudou).

DB isolado: SQLite in-memory (mesmo padrão de test_cadencia_enroll_followup).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

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


AGORA = datetime.now(timezone.utc)
CORRETOR_ID = 3
CORRETOR_KOMMO_USER = 10000003


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
        s.add(
            Corretora(
                id=CORRETOR_ID, nome="Pessoa3", telefone="+5500000000000",
                kommo_user_id=CORRETOR_KOMMO_USER, evolution_instance="acme_corretora_a",
                ativo=True, papel="corretora_atendedora_de_dona",
            )
        )
        s.commit()
    return factory


def _dados(**over):
    base = {
        "lead_id": "kommo_800",
        "nome": "Lead Manual",
        "telefone": "+5500000000000",
        "corretora_id": CORRETOR_ID,
        "etapa_nome": "Follow-up",
        "origem": "meta_ads",
    }
    base.update(over)
    return base


def _seed_lead_com_manual(session_factory, manual_at: datetime | None):
    """Cria o lead via sombra em Follow-up e simula o move manual pra
    'Visita marcada' (leads.stage_id + evento stage_manual_v2 em manual_at)."""
    upsert_lead_shadow(_dados(), session_factory=session_factory)
    with session_factory() as s:
        lead = s.query(Lead).filter(Lead.external_ref == "kommo_800").one()
        destino = PipelineStage(tenant_id=1, nome="Visita marcada", ordem=4)
        s.add(destino)
        s.flush()
        stage_manual_id = destino.id
        lead.stage_id = stage_manual_id
        if manual_at is not None:
            s.add(
                LeadEvent(
                    tenant_id=1, lead_id=lead.id, tipo="stage_manual_v2",
                    payload={"origem": None, "destino": stage_manual_id, "quem": "corretor"},
                    at=manual_at,
                )
            )
        s.commit()
        return lead.id, stage_manual_id


def _stage_do_lead(session_factory, lead_id: int) -> str | None:
    with session_factory() as s:
        lead = s.get(Lead, lead_id)
        if lead.stage_id is None:
            return None
        return s.get(PipelineStage, lead.stage_id).nome


def test_manual_recente_vence_kommo(session_factory):
    lead_id, _ = _seed_lead_com_manual(session_factory, manual_at=AGORA)
    # Kommo (Inspector da manhã) ainda acha que o lead está em Follow-up
    upsert_lead_shadow(_dados(etapa_nome="Follow-up"), session_factory=session_factory)
    assert _stage_do_lead(session_factory, lead_id) == "Visita marcada"
    with session_factory() as s:
        # nenhum stage_change fantasma registrado pela tentativa preservada
        assert (
            s.query(LeadEvent)
            .filter(LeadEvent.lead_id == lead_id, LeadEvent.tipo == "stage_change")
            .count()
            == 0
        )


def test_manual_antigo_nao_protege(session_factory):
    velho = AGORA - timedelta(hours=73)
    lead_id, _ = _seed_lead_com_manual(session_factory, manual_at=velho)
    upsert_lead_shadow(_dados(etapa_nome="Follow-up"), session_factory=session_factory)
    assert _stage_do_lead(session_factory, lead_id) == "Follow-up"
    with session_factory() as s:
        assert (
            s.query(LeadEvent)
            .filter(LeadEvent.lead_id == lead_id, LeadEvent.tipo == "stage_change")
            .count()
            == 1
        )


def test_sem_evento_manual_sombra_manda(session_factory):
    lead_id, _ = _seed_lead_com_manual(session_factory, manual_at=None)
    upsert_lead_shadow(_dados(etapa_nome="Follow-up"), session_factory=session_factory)
    assert _stage_do_lead(session_factory, lead_id) == "Follow-up"


def test_preservacao_nao_bloqueia_outros_campos(session_factory):
    lead_id, _ = _seed_lead_com_manual(session_factory, manual_at=AGORA)
    upsert_lead_shadow(
        _dados(etapa_nome="Follow-up", nome="Nome Novo", price="250000"),
        session_factory=session_factory,
    )
    with session_factory() as s:
        lead = s.get(Lead, lead_id)
        assert lead.nome == "Nome Novo"
        assert float(lead.valor) == 250000.0
    assert _stage_do_lead(session_factory, lead_id) == "Visita marcada"


def test_preservado_nao_dispara_enroll_followup(session_factory):
    """Kommo mandando Follow-up com manual recente NÃO matricula na cadência
    (o stage não mudou de verdade — foi preservado)."""
    lead_id, _ = _seed_lead_com_manual(session_factory, manual_at=AGORA)
    with patch.object(settings, "cadencia_auto_enroll", True):
        upsert_lead_shadow(
            _dados(etapa_nome="Follow-up"), session_factory=session_factory
        )
    with session_factory() as s:
        assert s.query(CadenciaEstado).count() == 0
    assert _stage_do_lead(session_factory, lead_id) == "Visita marcada"
