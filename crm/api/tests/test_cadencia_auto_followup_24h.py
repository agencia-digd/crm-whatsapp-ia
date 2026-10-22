"""Testes da AUTOMAÇÃO 24h (regra Dona 10/07) — cadencia.auto_followup_24h.

Regra: lead em "Em atendimento" cuja última mensagem trocada (mensagens_whatsapp,
match por sufixo-8 do telefone) tem 24h+ E o último remetente somos NÓS
(from_corretora=True — lead MUDO) → move pra Follow-up + lead_events
(tipo='auto_followup_24h'). Se o LEAD falou por último, a corretora é quem deve
responder — NÃO move. Só leads da Pessoa12, cap de segurança, atrás de flag.

Precisa de Postgres REAL (o SQL usa regexp_replace/DISTINCT ON):
    TEST_DATABASE_URL=postgresql+psycopg://user:CHANGE_ME@localhost:25440/copiloto

Isolamento: tenant_id dedicado (7724) + telefones sintéticos exclusivos —
nada aqui encosta em dado de outro teste nem, jamais, em produção (conftest
redireciona DATABASE_URL pro DB de teste antes de importar `app`).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="precisa de TEST_DATABASE_URL (Postgres de teste) — SQL usa regexp/DISTINCT ON",
)

TENANT = 7724  # tenant dedicado deste arquivo (não colide com o default 1)
CORRETOR_OWNER = 3  # id interno da Pessoa12 no seed do conftest
CORRETOR_KOMMO_USER = 10000003  # kommo_user_id da Pessoa12 no seed do conftest
CORRETOR_OWNER = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture()
def db():
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        yield s
        s.rollback()
        _limpar(s)
        s.commit()


def _limpar(s) -> None:
    from sqlalchemy import text

    s.execute(
        text("DELETE FROM cadencia_eventos WHERE tenant_id = :t"), {"t": TENANT}
    )
    s.execute(text("DELETE FROM cadencia_estado WHERE tenant_id = :t"), {"t": TENANT})
    s.execute(text("DELETE FROM lead_events WHERE tenant_id = :t"), {"t": TENANT})
    s.execute(text("DELETE FROM leads WHERE tenant_id = :t"), {"t": TENANT})
    s.execute(text("DELETE FROM pipeline_stages WHERE tenant_id = :t"), {"t": TENANT})
    s.execute(
        text("DELETE FROM mensagens_whatsapp WHERE tenant_id = :t"), {"t": TENANT}
    )


@pytest.fixture()
def stages(db):
    from app.db.models import PipelineStage

    em_atend = PipelineStage(tenant_id=TENANT, nome="Em atendimento", ordem=2)
    followup = PipelineStage(tenant_id=TENANT, nome="Follow-up", ordem=3)
    db.add_all([em_atend, followup])
    db.flush()
    db.commit()
    return {"em_atendimento": em_atend.id, "followup": followup.id}


_seq = iter(range(100, 999))


def _fone() -> str:
    """Telefone sintético único (sufixo-8 exclusivo por teste)."""
    return f"+5500000000000{next(_seq):03d}{uuid.uuid4().int % 10}"


def _mk_lead(db, stages, *, telefone: str, owner: int = CORRETOR_OWNER, nome: str = "Lead Teste"):
    from app.db.models import Lead

    lead = Lead(
        tenant_id=TENANT,
        external_ref=f"t24-{uuid.uuid4().hex[:10]}",
        nome=nome,
        telefone=telefone,
        owner_id=owner,
        stage_id=stages["em_atendimento"],
    )
    db.add(lead)
    db.flush()
    return lead


def _mk_msg(db, *, telefone: str, from_corretora: bool, ha_horas: float) -> None:
    from app.db.models import MensagemWhatsapp

    digits = "".join(ch for ch in telefone if ch.isdigit())
    db.add(
        MensagemWhatsapp(
            tenant_id=TENANT,
            evolution_message_id=f"t24-{uuid.uuid4().hex}",
            instance="acme_corretora_a",
            remote_jid=f"{digits}@s.whatsapp.net",
            contato_telefone=digits,
            from_corretora=from_corretora,
            tipo_mensagem="conversation",
            conteudo_texto="msg de teste",
            timestamp_evolution=_now() - timedelta(hours=ha_horas),
        )
    )
    db.flush()


def _rodar(db, **kw):
    from app.services import cadencia

    with patch.object(cadencia.settings, "cadencia_auto_followup_24h", True):
        return cadencia.auto_followup_24h(session=db, tenant_id=TENANT, **kw)


def _stage_de(db, lead) -> int:
    db.refresh(lead)
    return lead.stage_id


def _eventos(db, lead) -> list:
    from app.db.models import LeadEvent

    return (
        db.query(LeadEvent)
        .filter(
            LeadEvent.tenant_id == TENANT,
            LeadEvent.lead_id == lead.id,
            LeadEvent.tipo == "auto_followup_24h",
        )
        .all()
    )


# ── critérios ─────────────────────────────────────────────────────────────────

def test_mudo_24h_move_pra_followup_e_registra_evento(db, stages):
    tel = _fone()
    lead = _mk_lead(db, stages, telefone=tel)
    _mk_msg(db, telefone=tel, from_corretora=True, ha_horas=30)  # nós, 30h atrás

    res = _rodar(db)

    assert res.ativo is True
    assert res.mudos == 1
    assert res.movidos == 1
    assert _stage_de(db, lead) == stages["followup"]
    evs = _eventos(db, lead)
    assert len(evs) == 1
    assert evs[0].payload["stage_de"] == stages["em_atendimento"]
    assert evs[0].payload["origem"] == "cadencia_tick"


def test_lead_falou_por_ultimo_nao_move(db, stages):
    """Se o lead falou por último (mesmo 24h+ atrás), a corretora é quem deve
    responder — mover pra Follow-up matricularia na cadência quem tá conversando."""
    tel = _fone()
    lead = _mk_lead(db, stages, telefone=tel)
    _mk_msg(db, telefone=tel, from_corretora=True, ha_horas=50)
    _mk_msg(db, telefone=tel, from_corretora=False, ha_horas=30)  # lead por último

    res = _rodar(db)

    assert res.mudos == 0
    assert res.movidos == 0
    assert _stage_de(db, lead) == stages["em_atendimento"]
    assert _eventos(db, lead) == []


def test_menos_de_24h_nao_move(db, stages):
    tel = _fone()
    lead = _mk_lead(db, stages, telefone=tel)
    _mk_msg(db, telefone=tel, from_corretora=True, ha_horas=5)  # ainda dentro da janela

    res = _rodar(db)

    assert res.movidos == 0
    assert _stage_de(db, lead) == stages["em_atendimento"]


def test_outra_corretora_nao_move(db, stages):
    """Só leads da Pessoa12 (owner 3 / kommo_user dela) — Pessoa10 fica fora do piloto."""
    tel = _fone()
    lead = _mk_lead(db, stages, telefone=tel, owner=CORRETOR_OWNER)
    _mk_msg(db, telefone=tel, from_corretora=True, ha_horas=30)

    res = _rodar(db)

    assert res.candidatos == 0
    assert res.movidos == 0
    assert _stage_de(db, lead) == stages["em_atendimento"]


def test_owner_kommo_user_id_da_corretor_tambem_vale(db, stages):
    """owner_id na espinha pode ser o kommo_user_id (webhook fino) — aceita."""
    tel = _fone()
    lead = _mk_lead(db, stages, telefone=tel, owner=CORRETOR_KOMMO_USER)
    _mk_msg(db, telefone=tel, from_corretora=True, ha_horas=30)

    res = _rodar(db)

    assert res.movidos == 1
    assert _stage_de(db, lead) == stages["followup"]


def test_sem_mensagem_nenhuma_nao_move(db, stages):
    """Sem thread no WhatsApp não há evidência de mudez — não move."""
    lead = _mk_lead(db, stages, telefone=_fone())

    res = _rodar(db)

    assert res.candidatos == 1
    assert res.mudos == 0
    assert _stage_de(db, lead) == stages["em_atendimento"]


def test_match_por_sufixo_8_com_formatos_diferentes(db, stages):
    """Kommo grava '+5515...' formatado; Evolution grava dígitos — o match é
    pelos últimos 8 dígitos."""
    lead = _mk_lead(db, stages, telefone="+5500000000000")
    _mk_msg(db, telefone="+5500000000000", from_corretora=True, ha_horas=30)

    res = _rodar(db)

    assert res.movidos == 1
    assert _stage_de(db, lead) == stages["followup"]


def test_cap_de_seguranca_limita_moves(db, stages):
    leads = []
    for _ in range(3):
        tel = _fone()
        leads.append(_mk_lead(db, stages, telefone=tel))
        _mk_msg(db, telefone=tel, from_corretora=True, ha_horas=30)

    res = _rodar(db, cap=2)

    assert res.mudos == 3
    assert res.movidos == 2
    movidos = sum(1 for l in leads if _stage_de(db, l) == stages["followup"])
    assert movidos == 2


def test_flag_desligada_e_noop(db, stages):
    from app.services import cadencia

    tel = _fone()
    lead = _mk_lead(db, stages, telefone=tel)
    _mk_msg(db, telefone=tel, from_corretora=True, ha_horas=30)

    # SEM patch da flag — default False
    res = cadencia.auto_followup_24h(session=db, tenant_id=TENANT)

    assert res.ativo is False
    assert res.movidos == 0
    assert _stage_de(db, lead) == stages["em_atendimento"]


def test_idempotente_segunda_rodada_nao_duplica(db, stages):
    tel = _fone()
    lead = _mk_lead(db, stages, telefone=tel)
    _mk_msg(db, telefone=tel, from_corretora=True, ha_horas=30)

    _rodar(db)
    res2 = _rodar(db)  # lead já está em Follow-up — sai do universo

    assert res2.movidos == 0
    assert len(_eventos(db, lead)) == 1


def test_move_dispara_auto_enroll_quando_flag_ligada(db, stages):
    """Mover pra Follow-up liga a cadência (auto-enroll) quando
    CADENCIA_AUTO_ENROLL=true — o coração da 'cadência invisível'."""
    from app.db.models import CadenciaEstado
    from app.services import cadencia

    tel = _fone()
    _mk_lead(db, stages, telefone=tel)
    _mk_msg(db, telefone=tel, from_corretora=True, ha_horas=30)

    with patch.object(cadencia.settings, "cadencia_auto_enroll", True):
        res = _rodar(db)

    assert res.movidos == 1
    est = (
        db.query(CadenciaEstado)
        .filter(CadenciaEstado.tenant_id == TENANT)
        .all()
    )
    assert len(est) == 1
    assert est[0].status == "ativa"


def test_sem_auto_enroll_move_mas_nao_matricula(db, stages):
    """Flag CADENCIA_AUTO_ENROLL desligada: o move acontece, a matrícula não."""
    from app.db.models import CadenciaEstado

    tel = _fone()
    lead = _mk_lead(db, stages, telefone=tel)
    _mk_msg(db, telefone=tel, from_corretora=True, ha_horas=30)

    from app.services import cadencia
    # flag OFF explícita — o teste não pode depender do .env do ambiente
    with patch.object(cadencia.settings, "cadencia_auto_enroll", False):
        res = _rodar(db)

    assert res.movidos == 1
    assert _stage_de(db, lead) == stages["followup"]
    n = db.query(CadenciaEstado).filter(CadenciaEstado.tenant_id == TENANT).count()
    assert n == 0
