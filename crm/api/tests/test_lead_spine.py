"""Testes do dual-write EM SOMBRA da espinha canônica do lead (Opção A).

Cobre:
  1. upsert idempotente por (tenant_id, external_ref) — 1 lead, N chamadas.
  2. mudança de stage gera lead_events(tipo='stage_change'); estável não gera.
  3. falha no shadow NÃO quebra o webhook Kommo (retorna 200).
  4. etapa Kommo é traduzida pro funil canônico de 8 (alias) e a sombra NUNCA
     cria coluna fora das 8 (item 3 blueprint 10/07).

DB isolado: SQLite in-memory. JSONB (dialeto Postgres) é compilado como JSON no
SQLite via @compiles, pra o mesmo Base.metadata das models rodar sem Postgres.
Zero acesso ao DATABASE_URL de produção.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Lead, LeadEvent, PipelineStage
from app.services.lead_spine import upsert_lead_shadow


# JSONB → JSON no SQLite (só afeta este processo de teste).
@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


# No SQLite só INTEGER PRIMARY KEY autoincrementa (rowid). Em Postgres é BIGSERIAL.
# Renderiza BigInteger como INTEGER só no SQLite pra os PKs autoincrementarem no teste.
@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite://")
    # Só as 3 tabelas da espinha (evita puxar todo o schema/Postgres-only).
    Base.metadata.create_all(
        engine,
        tables=[Lead.__table__, PipelineStage.__table__, LeadEvent.__table__],
    )
    return sessionmaker(bind=engine)


def _lead(**over):
    base = {
        "lead_id": "kommo_555",
        "nome": "João Silva",
        "telefone": "+5500000000000",
        "corretora_id": 3,
        "etapa_nome": "Primeiro contato",
        "origem": "meta_ads",
        "price": 350000,
    }
    base.update(over)
    return base


# ── 1. Idempotência ────────────────────────────────────────────────────────────

def test_upsert_idempotente(session_factory):
    for _ in range(3):
        lid = upsert_lead_shadow(_lead(), session_factory=session_factory)
    assert lid is not None

    with session_factory() as s:
        leads = s.query(Lead).all()
        assert len(leads) == 1, "3 chamadas iguais devem convergir pra 1 lead"
        lead = leads[0]
        assert lead.external_ref == "kommo_555"
        assert lead.tenant_id == 1
        assert lead.owner_id == 3
        assert lead.origem == "meta_ads"
        assert lead.valor == Decimal("350000")

        # 1 stage no catálogo (não duplica) + 1 evento (só o lead_created).
        # "Primeiro contato" (nome Kommo) vira a coluna canônica "Novo".
        assert s.query(PipelineStage).count() == 1
        assert s.query(PipelineStage).one().nome == "Novo"
        eventos = s.query(LeadEvent).all()
        assert len(eventos) == 1
        assert eventos[0].tipo == "lead_created"


def test_upsert_atualiza_campos_sem_duplicar(session_factory):
    upsert_lead_shadow(_lead(nome="João"), session_factory=session_factory)
    upsert_lead_shadow(_lead(nome="João Silva Jr"), session_factory=session_factory)

    with session_factory() as s:
        assert s.query(Lead).count() == 1
        assert s.query(Lead).one().nome == "João Silva Jr"


# ── valor de negócio (price do Kommo) ──────────────────────────────────────────

def test_valor_do_price_kommo(session_factory):
    upsert_lead_shadow(_lead(price="425000.50"), session_factory=session_factory)
    with session_factory() as s:
        assert s.query(Lead).one().valor == Decimal("425000.50")


def test_valor_ausente_ou_invalido_e_none(session_factory):
    # Telefones DIFERENTES de propósito: desde o dedupe por telefone canônico
    # (L3), dois refs do Kommo com o MESMO telefone convergem pro mesmo lead —
    # que é o comportamento correto. Aqui o alvo do teste é o parse do `price`,
    # então cada caso precisa da sua própria pessoa.
    upsert_lead_shadow(
        _lead(price=None, valor=None, lead_id="kommo_semvalor",
              telefone="+5500000000000"),
        session_factory=session_factory,
    )
    upsert_lead_shadow(
        _lead(price="abc", valor=None, lead_id="kommo_lixo",
              telefone="+5500000000000"),
        session_factory=session_factory,
    )
    with session_factory() as s:
        vals = {l.external_ref: l.valor for l in s.query(Lead).all()}
        assert vals["kommo_semvalor"] is None
        assert vals["kommo_lixo"] is None


def test_valor_atualiza_sem_duplicar(session_factory):
    upsert_lead_shadow(_lead(price=100000), session_factory=session_factory)
    upsert_lead_shadow(_lead(price=150000), session_factory=session_factory)
    with session_factory() as s:
        assert s.query(Lead).count() == 1
        assert s.query(Lead).one().valor == Decimal("150000")


# ── 2. Mudança de stage gera evento ────────────────────────────────────────────

def test_mudanca_stage_gera_evento(session_factory):
    upsert_lead_shadow(_lead(etapa_nome="Primeiro contato"), session_factory=session_factory)
    upsert_lead_shadow(_lead(etapa_nome="Visita agendada"), session_factory=session_factory)

    with session_factory() as s:
        eventos = s.query(LeadEvent).order_by(LeadEvent.id).all()
        tipos = [e.tipo for e in eventos]
        assert tipos == ["lead_created", "stage_change"], tipos

        change = eventos[1]
        # 2 stages distintos no catálogo — já traduzidos pro funil canônico
        stages = {st.id: st.nome for st in s.query(PipelineStage).all()}
        assert set(stages.values()) == {"Novo", "Visita marcada"}
        assert change.payload["de"] != change.payload["para"]

        lead = s.query(Lead).one()
        assert stages[lead.stage_id] == "Visita marcada"


def test_stage_estavel_nao_gera_evento(session_factory):
    upsert_lead_shadow(_lead(etapa_nome="Primeiro contato"), session_factory=session_factory)
    # mesma etapa, muda só o nome → nenhum stage_change
    upsert_lead_shadow(
        _lead(etapa_nome="Primeiro contato", nome="Outro Nome"),
        session_factory=session_factory,
    )

    with session_factory() as s:
        tipos = [e.tipo for e in s.query(LeadEvent).all()]
        assert tipos == ["lead_created"], tipos


def test_etapa_id_cru_do_followup_vira_follow_up(session_factory):
    """"10000001" (etapa_id do Follow-up Kommo sem nome resolvido — órfã vista
    em produção) mapeia pra coluna canônica "Follow-up"."""
    upsert_lead_shadow(
        _lead(etapa_nome=None, etapa_id="10000001"), session_factory=session_factory
    )
    with session_factory() as s:
        assert s.query(PipelineStage).one().nome == "Follow-up"


def test_etapa_fora_do_funil_nao_cria_coluna(session_factory):
    """Etapa desconhecida (fora do funil de 8 e sem alias) NÃO vira coluna
    nova — lead fica sem stage ("Sem etapa" no board)."""
    lid = upsert_lead_shadow(
        _lead(etapa_nome="Coluna Inventada"), session_factory=session_factory
    )
    assert lid is not None
    with session_factory() as s:
        assert s.query(PipelineStage).count() == 0
        assert s.query(Lead).one().stage_id is None


# ── Kill-switch (reversibilidade) ──────────────────────────────────────────────

def test_shadow_desligado_e_noop(session_factory):
    with patch("app.services.lead_spine.settings.lead_spine_shadow", False):
        lid = upsert_lead_shadow(_lead(), session_factory=session_factory)
    assert lid is None
    with session_factory() as s:
        assert s.query(Lead).count() == 0


def test_sem_external_ref_e_noop(session_factory):
    lid = upsert_lead_shadow({"nome": "sem id"}, session_factory=session_factory)
    assert lid is None
    with session_factory() as s:
        assert s.query(Lead).count() == 0


# ── 3. Falha no shadow não quebra o webhook Kommo ──────────────────────────────

def test_falha_shadow_nao_quebra_webhook():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.kommo_webhook import router
    from app.config import settings

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    payload = {"leads": {"add": [{"id": "kommo_999", "name": "Pessoa", "status_id": 10}]}}

    with patch.object(settings, "kommo_webhook_secret", "s3cr3t"), \
         patch("app.services.lead_spine.upsert_lead_shadow", side_effect=RuntimeError("boom")), \
         patch("app.api.kommo_webhook.announce") as mock_announce:
        mock_announce.return_value = None
        # announce é async — retorna coroutine; usa AsyncMock-like via patch simples
        async def _noop(_):  # noqa: ANN001, ANN202
            return True
        mock_announce.side_effect = _noop

        resp = client.post("/webhook/kommo/lead-created/s3cr3t", json=payload)

    # shadow explodiu, mas o webhook segue 200 (best-effort)
    assert resp.status_code == 200
