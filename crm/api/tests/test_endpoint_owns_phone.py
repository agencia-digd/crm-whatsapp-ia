"""Testes do GET /assistente/owns_phone — Onda 2.

Telefones vêm das constantes de `tests/conftest.py` (fonte única do seed) —
ver a nota em test_sender_lookup.py sobre o literal do número da Pessoa12.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

from tests.conftest import (
    TEL_DANIELA_REMOVIDA,
    TEL_DESCONHECIDO,
    TEL_DONA,
    TEL_CORRETORA_A,
)


@pytest.fixture
def client():
    return TestClient(app)


def _hdrs():
    return {"X-Internal-Token": settings.copiloto_internal_token}


def test_corretora_a(client):
    r = client.get("/assistente/owns_phone", params={"phone": TEL_CORRETORA_A}, headers=_hdrs())
    assert r.status_code == 200
    body = r.json()
    assert body["owns"] is True
    assert body["kind"] == "corretora"
    assert body["nome"] == "Pessoa3"


def test_pessoa(client):
    r = client.get("/assistente/owns_phone", params={"phone": TEL_DONA}, headers=_hdrs())
    assert r.status_code == 200
    assert r.json()["kind"] == "dona"


def test_daniela_removida(client):
    """Onda P1: Dona saiu de internos_acme."""
    r = client.get("/assistente/owns_phone", params={"phone": TEL_DANIELA_REMOVIDA}, headers=_hdrs())
    assert r.status_code == 200
    assert r.json() == {"owns": False}


def test_aleatorio(client):
    r = client.get("/assistente/owns_phone", params={"phone": TEL_DESCONHECIDO}, headers=_hdrs())
    assert r.status_code == 200
    assert r.json() == {"owns": False}


def test_sem_token_401(client):
    r = client.get("/assistente/owns_phone", params={"phone": TEL_DONA})
    assert r.status_code == 401


# ─── Lead da Acme (incidente da corretor, 02/08/2026) ──────────────────────────
# O bridge da agencia usa este endpoint pra decidir quem responde. Lead NÃO é
# `owns` (a Assistente atende equipe), mas precisa ser reconhecido: sem isso ele
# cai na corretor, atendente da AGÊNCIA, e ouve "a gente não vende imóveis".

TEL_LEAD_ACME = "+5500000000000"


@pytest.fixture
def lead_seed():
    """Cria um lead com telefone conhecido e remove no fim."""
    from sqlalchemy import text as sql_text

    from app.db.session import SessionLocal

    with SessionLocal() as s:
        s.execute(
            sql_text("INSERT INTO leads (tenant_id, external_ref, nome, telefone) "
                     "VALUES (1, :ref, :nome, :tel)"),
            {"ref": "teste-corretor-9001", "nome": "Elias Teste",
             "tel": f"+{TEL_LEAD_ACME}"},
        )
        s.commit()
    yield TEL_LEAD_ACME
    with SessionLocal() as s:
        s.execute(sql_text("DELETE FROM leads WHERE external_ref = :ref"),
                  {"ref": "teste-corretor-9001"})
        s.commit()


def test_lead_da_acme_e_reconhecido(client, lead_seed):
    r = client.get("/assistente/owns_phone", params={"phone": lead_seed},
                   headers=_hdrs())
    assert r.status_code == 200
    body = r.json()
    assert body["lead"] is True
    assert body["nome"] == "Elias Teste"


def test_lead_nao_e_owns(client, lead_seed):
    """`owns` dispara /assistente/run (Assistente atende CORRETORA). Lead não pode."""
    r = client.get("/assistente/owns_phone", params={"phone": lead_seed},
                   headers=_hdrs())
    assert r.json()["owns"] is False


def test_corretora_continua_owns_sem_flag_de_lead(client):
    """Regressão: o caminho da corretora não pode ter mudado."""
    r = client.get("/assistente/owns_phone", params={"phone": TEL_CORRETORA_A},
                   headers=_hdrs())
    body = r.json()
    assert body["owns"] is True and body["kind"] == "corretora"
    assert "lead" not in body


def test_desconhecido_de_verdade_nao_vira_lead(client):
    """Quem não é nada continua indo pra corretor — ela ainda atende a agência."""
    r = client.get("/assistente/owns_phone", params={"phone": TEL_DESCONHECIDO},
                   headers=_hdrs())
    assert r.json() == {"owns": False}
