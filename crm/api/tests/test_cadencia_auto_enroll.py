"""Testes do AUTO-ENROLL da cadência (webhook Kommo → linha da Pessoa12).

Cobre:
  - flag `cadencia_auto_enroll` desligada (default) → não faz NADA;
  - lead novo atribuído à Pessoa12 (kommo_user_id via tabela corretoras) → matricula;
  - lead de outra corretora → ignora;
  - lead sem telefone (webhook fino) ou telefone inválido → ignora;
  - idempotência: 2 webhooks do mesmo telefone → 1 cadência só;
  - telefone legado do Kommo com '0' na frente do DDD → normaliza;
  - falha na matrícula NÃO derruba o webhook (best-effort, padrão lead_spine).

DB isolado: SQLite in-memory (mesmo padrão de test_cadencia/test_lead_spine).
Zero prod, zero envio — matrícula só grava estado; quem toca é o tick (DRY-RUN).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.api.kommo_webhook import (
    _auto_enroll_cadencia_corretor,
    _extract_lead_phone,
    _normalizar_telefone_kommo,
)
from app.config import settings
from app.db.models import Base, CadenciaEstado, CadenciaEvento, Corretora


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


# Mesmos valores canônicos do seed de tests/conftest.py / painel.py
CORRETOR_ID = 3
CORRETOR_KOMMO_USER = 10000003
CORRETOR_KOMMO_USER = 10000006


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[Corretora.__table__, CadenciaEstado.__table__, CadenciaEvento.__table__],
    )
    factory = sessionmaker(bind=engine)
    with factory() as s:
        s.add_all(
            [
                Corretora(
                    id=1, nome="Pessoa5", telefone="+5500000000000",
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


def _lead(telefone: str | None = "+5500000000000", **over):
    """Payload de lead como chega no bloco leads[add][0] do webhook Kommo."""
    lead: dict = {
        "id": "kommo_777",
        "name": "Lead Teste",
        "status_id": 10,
        "responsible_user_id": CORRETOR_KOMMO_USER,
    }
    if telefone is not None:
        lead["custom_fields"] = [
            {"id": "101", "name": "Telefone", "code": "PHONE",
             "values": [{"value": telefone}]},
        ]
    lead.update(over)
    return lead


def _cadencias(session_factory) -> list[CadenciaEstado]:
    with session_factory() as s:
        return s.query(CadenciaEstado).all()


# ── 1. Flag desligada (default) = não faz nada ────────────────────────────────

def test_flag_desligada_nao_matricula(session_factory):
    # flag OFF explícita — o default do settings é False, mas o .env de dev/prod pode ligar CADENCIA_AUTO_ENROLL; o teste não pode depender do ambiente
    with patch.object(settings, "cadencia_auto_enroll", False):
        cid = _auto_enroll_cadencia_corretor(_lead(), session_factory=session_factory)
    assert cid is None
    assert _cadencias(session_factory) == []


# ── 2. Lead novo da Pessoa12 → matricula ────────────────────────────────────────────

def test_matricula_lead_da_corretor(session_factory, flag_ligada):
    cid = _auto_enroll_cadencia_corretor(_lead(), session_factory=session_factory)
    assert cid is not None

    rows = _cadencias(session_factory)
    assert len(rows) == 1
    est = rows[0]
    assert est.telefone == "+5500000000000"      # normalizado (E.164 sem '+')
    assert est.corretora_id == CORRETOR_ID
    assert est.lead_id == "kommo_777"
    assert est.status == "ativa"
    assert est.toque_atual == 0


# ── 3. Lead de outra corretora → ignora ───────────────────────────────────────

def test_ignora_lead_de_outra_corretora(session_factory, flag_ligada):
    cid = _auto_enroll_cadencia_corretor(
        _lead(responsible_user_id=CORRETOR_KOMMO_USER), session_factory=session_factory
    )
    assert cid is None
    assert _cadencias(session_factory) == []


def test_ignora_owner_desconhecido_ou_ausente(session_factory, flag_ligada):
    assert _auto_enroll_cadencia_corretor(
        _lead(responsible_user_id=99999999), session_factory=session_factory
    ) is None
    assert _auto_enroll_cadencia_corretor(
        _lead(responsible_user_id=None), session_factory=session_factory
    ) is None
    assert _cadencias(session_factory) == []


# ── 4. Sem telefone / telefone inválido → ignora ─────────────────────────────

def test_ignora_lead_sem_telefone(session_factory, flag_ligada):
    cid = _auto_enroll_cadencia_corretor(_lead(telefone=None), session_factory=session_factory)
    assert cid is None
    assert _cadencias(session_factory) == []


def test_ignora_telefone_invalido(session_factory, flag_ligada):
    cid = _auto_enroll_cadencia_corretor(_lead(telefone="999"), session_factory=session_factory)
    assert cid is None
    assert _cadencias(session_factory) == []


# ── 5. Idempotência: 2 webhooks, 1 cadência ──────────────────────────────────

def test_nao_duplica_matricula(session_factory, flag_ligada):
    cid1 = _auto_enroll_cadencia_corretor(_lead(), session_factory=session_factory)
    cid2 = _auto_enroll_cadencia_corretor(
        _lead(id="kommo_888"), session_factory=session_factory  # outro lead, MESMO telefone
    )
    assert cid1 == cid2
    assert len(_cadencias(session_factory)) == 1


# ── 6. Normalização: formato legado do Kommo com '0' na frente ───────────────

def test_telefone_legado_com_zero_normaliza(session_factory, flag_ligada):
    cid = _auto_enroll_cadencia_corretor(
        _lead(telefone="+5500000000000"), session_factory=session_factory
    )
    assert cid is not None
    rows = _cadencias(session_factory)
    assert rows[0].telefone == "+5500000000000"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+5500000000000", "+5500000000000"),
        ("5500000000000", "+5500000000000"),
        ("+5500000000000", "+5500000000000"),   # legado Kommo: 0 + DDD
        ("0+5500000000000", "+5500000000000"),
        ("", None),
        (None, None),
        ("abc", None),
        ("999", None),
    ],
)
def test_normalizar_telefone_kommo(raw, expected):
    assert _normalizar_telefone_kommo(raw) == expected


def test_extract_lead_phone_formatos():
    # formato webhook (custom_fields + code)
    assert _extract_lead_phone(_lead()) == "+5500000000000"
    # formato API (custom_fields_values + field_code)
    lead_api = {
        "responsible_user_id": CORRETOR_KOMMO_USER,
        "custom_fields_values": [
            {"field_code": "PHONE", "values": [{"value": "+5500000000000"}]}
        ],
    }
    assert _extract_lead_phone(lead_api) == "+5500000000000"
    # sem campo de telefone
    assert _extract_lead_phone({"custom_fields": [{"name": "Origem", "values": [{"value": "Meta"}]}]}) is None


# ── 7. Best-effort: falha na matrícula NÃO derruba o webhook ─────────────────

def test_falha_auto_enroll_nao_quebra_webhook():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.kommo_webhook import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    payload = {"leads": {"add": [_lead()]}}

    async def _noop(_):  # noqa: ANN001, ANN202
        return True

    with patch.object(settings, "kommo_webhook_secret", "s3cr3t"), \
         patch("app.api.kommo_webhook._auto_enroll_cadencia_corretor",
               side_effect=RuntimeError("boom")), \
         patch("app.services.lead_spine.upsert_lead_shadow", return_value=None), \
         patch("app.api.kommo_webhook.announce", side_effect=_noop):
        resp = client.post("/webhook/kommo/lead-created/s3cr3t", json=payload)

    # matrícula explodiu, mas o webhook segue 200 (best-effort)
    assert resp.status_code == 200
