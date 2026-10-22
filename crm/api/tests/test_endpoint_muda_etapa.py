"""POST /painel/lead/{lead_id}/etapa — escrita de etapa (Missão 11).

Tudo mockado: stage_map (stages válidos), TwentyClient (opp + update_stage),
e a resolução lead→corretora. Sem banco, sem rede, sem Twenty real.

Casos: 400 (stage inválido/ausente), 403 (corretora de outro lead / sem cid),
404 (lead fora do piloto Twenty), happy (admin e corretora dona).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.api.painel as painel
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _mock_stage_map(monkeypatch):
    # stages válidos fixos (não bate no banco). Usa os usuários reais do painel
    # (dona=admin, corretor=corretora 3) — _identificar_usuario NÃO é mockado.
    from app.services import stage_map as sm
    monkeypatch.setattr(sm, "stages_validos", lambda: {"NEW", "VISITA_REAL", "PROPOSAL"})


class _FakeTwenty:
    """TwentyClient dublê: opp configurável + captura do update."""

    opp_por_kommo: dict | None = {"id": "opp-1", "kommoLeadId": "555", "stage": "NEW"}
    updated: list = []

    def __init__(self, *a, **k):
        pass

    def get_opportunity_by_kommo_id(self, kid):
        return type(self).opp_por_kommo

    def update_stage(self, opp_id, novo):
        type(self).updated.append((opp_id, novo))
        return {"id": opp_id, "stage": novo}


@pytest.fixture(autouse=True)
def _mock_twenty(monkeypatch):
    _FakeTwenty.opp_por_kommo = {"id": "opp-1", "kommoLeadId": "555", "stage": "NEW"}
    _FakeTwenty.updated = []
    import app.integrations.twenty_client as tcmod
    monkeypatch.setattr(tcmod, "TwentyClient", _FakeTwenty)


def _hdr(user):
    return {"X-Painel-User": user}


# ── 400 ──────────────────────────────────────────────────────────────────────

def test_400_sem_stage(client):
    r = client.post("/painel/lead/555/etapa", json={}, headers=_hdr("dona"))
    assert r.status_code == 400
    assert "novo_stage_obrigatorio" in r.text


def test_400_stage_invalido(client):
    r = client.post("/painel/lead/555/etapa", json={"novo_stage": "ZZZ"}, headers=_hdr("dona"))
    assert r.status_code == 400
    assert "stage_invalido" in r.text


# ── 403 ──────────────────────────────────────────────────────────────────────

def test_403_corretora_de_outro_lead(client, monkeypatch):
    # corretor = corretora 3; lead pertence à corretora 1 → 403
    monkeypatch.setattr(painel, "_corretora_id_do_lead", lambda lid: 1)
    r = client.post("/painel/lead/555/etapa", json={"novo_stage": "VISITA_REAL"}, headers=_hdr("corretor"))
    assert r.status_code == 403
    assert "lead_de_outra_corretora" in r.text


def test_403_lead_sem_dono_pra_corretora(client, monkeypatch):
    monkeypatch.setattr(painel, "_corretora_id_do_lead", lambda lid: None)
    r = client.post("/painel/lead/555/etapa", json={"novo_stage": "VISITA_REAL"}, headers=_hdr("corretor"))
    assert r.status_code == 403
    assert "lead_sem_corretora" in r.text


# ── 404 ──────────────────────────────────────────────────────────────────────

def test_404_lead_fora_do_piloto(client):
    _FakeTwenty.opp_por_kommo = None  # não existe no Twenty
    r = client.post("/painel/lead/999/etapa", json={"novo_stage": "VISITA_REAL"}, headers=_hdr("dona"))
    assert r.status_code == 404
    assert "lead_fora_do_piloto" in r.text


# ── happy ─────────────────────────────────────────────────────────────────────

def test_happy_admin(client):
    r = client.post("/painel/lead/555/etapa", json={"novo_stage": "VISITA_REAL"}, headers=_hdr("dona"))
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True, "de": "NEW", "para": "VISITA_REAL"}
    assert _FakeTwenty.updated == [("opp-1", "VISITA_REAL")]


def test_happy_corretora_dona(client, monkeypatch):
    # corretor = corretora 3; lead pertence a 3 → ok
    monkeypatch.setattr(painel, "_corretora_id_do_lead", lambda lid: 3)
    r = client.post("/painel/lead/555/etapa", json={"novo_stage": "PROPOSAL"}, headers=_hdr("corretor"))
    assert r.status_code == 200
    assert r.json()["para"] == "PROPOSAL"


def test_noop_quando_mesmo_stage(client):
    # opp já está em NEW; pedir NEW → noop, sem update
    r = client.post("/painel/lead/555/etapa", json={"novo_stage": "NEW"}, headers=_hdr("dona"))
    assert r.status_code == 200
    body = r.json()
    assert body["noop"] is True
    assert _FakeTwenty.updated == []
