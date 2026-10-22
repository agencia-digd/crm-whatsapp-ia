"""Testes do motor de NUTRIÇÃO (funil remarketing — spec Dona 10/07).

Cobre:
  - ENTRADA: lead que esgotou a cadência (status='remarketing') entra na etapa Base;
  - MARCOS: cada disparo (1/30 dias) avança a etapa (Base→30→60→90→120→150) e a
    tag do card troca (30D→60D→90D→120D→150D);
  - 150 PARA: depois do 5º disparo o lead fica na etapa "150 dias" e nada mais sai;
  - coexistência: lead processado pela nutrição NÃO recebe o remarketing genérico
    do cadencia.tick no mesmo dia (proximo_toque_em re-agendado);
  - copies: linha única; oficiais Kommo marcadas # APROVADO DONA 10/07 no fonte,
    rascunhos (30/150) marcados # RASCUNHO; 30 dias fecha sem pergunta
    (decisão Dona 11/07), 150 dias termina com pergunta.

DB isolado: SQLite in-memory (mesmo padrão de test_cadencia). Sender fake —
nunca chama o motor real da Fase 7.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base,
    CadenciaEstado,
    CadenciaEvento,
    DisparoReengajamento,
    Lead,
    LeadEvent,
    PipelineStage,
)
from app.services import cadencia, nutricao


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            CadenciaEstado.__table__,
            CadenciaEvento.__table__,
            Lead.__table__,
            LeadEvent.__table__,
            PipelineStage.__table__,
            # opt-out (07/10): a nutrição lê disparos_reengajamento a cada
            # volta e, sem a tabela, segura todo marco (fail-closed).
            DisparoReengajamento.__table__,
        ],
    )
    return sessionmaker(bind=engine)


@pytest.fixture(autouse=True)
def _reset_guard():
    cadencia._LAST_ENQUEUE["at"] = 0.0
    yield
    cadencia._LAST_ENQUEUE["at"] = 0.0


class FakeSender:
    def __init__(self):
        self.chamadas: list[dict] = []

    def __call__(self, telefone, texto, *, com_botao):
        self.chamadas.append(
            {"telefone": telefone, "texto": texto, "com_botao": com_botao}
        )
        return {"ok": True, "dry_run": True, "sent": False}


T0 = datetime(2026, 7, 10, 15, 0, tzinfo=timezone.utc)
FONE = "+5500000000000"


def _seed_remarketing(s, *, proximo=None, lead_stage=None):
    """Estado pós-cadência (esgotou os 8 toques) + lead canônico correspondente."""
    est = CadenciaEstado(
        tenant_id=1,
        telefone=FONE,
        toque_atual=8,
        status="remarketing",
        proximo_toque_em=proximo,
        criado_em=T0 - timedelta(days=40),
        atualizado_em=T0,
    )
    lead = Lead(tenant_id=1, nome="Lead Nutrição", telefone=f"+{FONE}", stage_id=lead_stage)
    s.add_all([est, lead])
    s.commit()
    return est, lead


def _stage_nome(s, stage_id):
    st = s.get(PipelineStage, stage_id)
    return (st.nome or "").strip() if st else None


# ── entrada ───────────────────────────────────────────────────────────────────

def test_entrada_pos_cadencia_vai_pra_base(session_factory):
    s = session_factory()
    est, lead = _seed_remarketing(s, proximo=T0 + timedelta(days=30))

    res = nutricao.tick(agora=T0, session=s, sender=FakeSender())

    assert res.entradas == 1
    s.refresh(lead)
    assert _stage_nome(s, lead.stage_id) == "Base"
    st = s.get(PipelineStage, lead.stage_id)
    assert st.funil == "remarketing"
    ev = s.query(LeadEvent).filter(LeadEvent.tipo == "nutricao_entrada").all()
    assert len(ev) == 1
    # sem marco devido → nenhuma mensagem
    assert res.marcos_disparados == 0


def test_entrada_idempotente(session_factory):
    s = session_factory()
    _seed_remarketing(s, proximo=T0 + timedelta(days=30))
    nutricao.tick(agora=T0, session=s, sender=FakeSender())
    res2 = nutricao.tick(agora=T0 + timedelta(hours=1), session=s, sender=FakeSender())
    assert res2.entradas == 0  # já está no funil


def test_cadencia_ativa_nao_entra_no_funil(session_factory):
    s = session_factory()
    est = CadenciaEstado(
        tenant_id=1, telefone=FONE, toque_atual=3, status="ativa",
        proximo_toque_em=T0, criado_em=T0, atualizado_em=T0,
    )
    lead = Lead(tenant_id=1, nome="Ainda em cadência", telefone=f"+{FONE}")
    s.add_all([est, lead])
    s.commit()

    res = nutricao.tick(agora=T0, session=s, sender=FakeSender())
    assert res.entradas == 0
    s.refresh(lead)
    assert lead.stage_id is None


# ── marcos ────────────────────────────────────────────────────────────────────

def test_marcos_avancam_etapa_e_tag_ate_150_e_para(session_factory):
    s = session_factory()
    sender = FakeSender()
    # devido JÁ no primeiro tick (cooldown de 30d venceu)
    est, lead = _seed_remarketing(s, proximo=T0)

    esperado = [
        ("30 dias", "30D"), ("60 dias", "60D"), ("90 dias", "90D"),
        ("120 dias", "120D"), ("150 dias", "150D"),
    ]
    agora = T0
    for i, (etapa, tag) in enumerate(esperado):
        cadencia._LAST_ENQUEUE["at"] = 0.0  # zera o guard entre "dias"
        res = nutricao.tick(agora=agora, session=s, sender=sender)
        assert res.marcos_disparados == 1, f"marco {etapa} não disparou"
        s.refresh(lead)
        assert _stage_nome(s, lead.stage_id) == etapa
        assert sender.chamadas[-1]["texto"] == nutricao.COPIES_MARCO[etapa]
        ev = (
            s.query(LeadEvent)
            .filter(LeadEvent.tipo == "nutricao_marco")
            .order_by(LeadEvent.id.desc())
            .first()
        )
        assert ev.payload["marco"] == etapa
        assert ev.payload["tag"] == tag
        s.refresh(est)
        if etapa == "150 dias":
            assert est.proximo_toque_em is None  # PAROU
        else:
            assert est.proximo_toque_em is not None
        agora = agora + timedelta(days=30)

    # 6º tick (180 dias): nada mais sai, lead FICA no 150 dias
    cadencia._LAST_ENQUEUE["at"] = 0.0
    res = nutricao.tick(agora=agora, session=s, sender=sender)
    assert res.marcos_disparados == 0
    assert len(sender.chamadas) == 5
    s.refresh(lead)
    assert _stage_nome(s, lead.stage_id) == "150 dias"


def test_marco_nao_dispara_antes_dos_30_dias(session_factory):
    s = session_factory()
    sender = FakeSender()
    _seed_remarketing(s, proximo=T0 + timedelta(days=30))
    res = nutricao.tick(agora=T0 + timedelta(days=29), session=s, sender=sender)
    assert res.marcos_disparados == 0
    assert sender.chamadas == []


def test_idempotente_no_mesmo_dia(session_factory):
    s = session_factory()
    sender = FakeSender()
    _seed_remarketing(s, proximo=T0)
    nutricao.tick(agora=T0, session=s, sender=sender)
    cadencia._LAST_ENQUEUE["at"] = 0.0
    res2 = nutricao.tick(agora=T0 + timedelta(hours=2), session=s, sender=sender)
    assert res2.marcos_disparados == 0
    assert len(sender.chamadas) == 1  # só o 1º tick mandou


# ── coexistência com o remarketing genérico da cadência ──────────────────────

def test_nutricao_reagenda_e_cadencia_nao_duplica(session_factory):
    s = session_factory()
    sender_nut = FakeSender()
    sender_cad = FakeSender()
    _seed_remarketing(s, proximo=T0)

    nutricao.tick(agora=T0, session=s, sender=sender_nut)
    assert len(sender_nut.chamadas) == 1

    cadencia._LAST_ENQUEUE["at"] = 0.0
    res = cadencia.tick(agora=T0, session=s, sender=sender_cad)
    assert res.remarketing == 0  # proximo_toque_em foi re-agendado pela nutrição
    assert sender_cad.chamadas == []


def test_sem_lead_canonico_fica_no_fluxo_generico(session_factory):
    s = session_factory()
    sender_nut = FakeSender()
    est = CadenciaEstado(
        tenant_id=1, telefone=FONE, toque_atual=8, status="remarketing",
        proximo_toque_em=T0, criado_em=T0 - timedelta(days=40), atualizado_em=T0,
    )
    s.add(est)
    s.commit()

    res = nutricao.tick(agora=T0, session=s, sender=sender_nut)
    assert res.marcos_disparados == 0
    assert res.sem_lead_canonico == 1
    s.refresh(est)
    # segue devido pro cadencia.tick (fluxo genérico antigo cuida dele)
    assert est.proximo_toque_em is not None


# ── copies (rascunhos pra Dona revisar) ───────────────────────────────────────

def test_copies_linha_unica_e_finais_dos_rascunhos():
    assert set(nutricao.COPIES_MARCO) == {
        "30 dias", "60 dias", "90 dias", "120 dias", "150 dias"
    }
    for marco, texto in nutricao.COPIES_MARCO.items():
        assert "\n" not in texto, f"{marco}: copy tem que ser linha única"
        assert texto.strip(), f"{marco}: copy vazia"
    # rascunhos (copy real do Kommo chega via mapeamento):
    # 30 dias fecha em afirmação de disponibilidade (decisão Dona 11/07 —
    # copy sem pergunta vale); 150 dias segue o padrão método Pessoa
    # e termina com pergunta.
    assert nutricao.COPIES_MARCO["30 dias"].rstrip().endswith(
        "do jeito que tem que ser."
    )
    assert nutricao.COPIES_MARCO["150 dias"].rstrip().endswith("?")


def test_seed_stages_do_funil(session_factory):
    s = session_factory()
    _seed_remarketing(s, proximo=None)
    nutricao.tick(agora=T0, session=s, sender=FakeSender())
    nomes = [
        st.nome
        for st in s.query(PipelineStage)
        .filter(PipelineStage.funil == "remarketing")
        .order_by(PipelineStage.ordem)
        .all()
    ]
    assert nomes == ["Base", "30 dias", "60 dias", "90 dias", "120 dias", "150 dias"]
