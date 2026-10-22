"""Testes de escopo de autorização da Assistente (Furos 1 e 2 — P0 2026-07-10).

Provam, sem depender de LLM/DB, que:
- `_maybe_inject_corretora` injeta `_for_corretora_id` nas tools de leitura quando
  o remetente é CORRETORA, e NÃO injeta quando é a DONA (que tem acesso amplo).
- `_fetch_painel` propaga a identidade no header `X-Painel-User`: corretora → o
  login dela; dona → "dona"; corretora desconhecida → NEGA (nem chama o painel).
"""
from __future__ import annotations

from app.agents.assistente.agent import (
    TOOLS_SCOPED_TO_CORRETORA,
    AgenteAssistente,
    _fetch_painel,
)


# ---- Furo 1 + 2: injeção do escopo por corretora no dispatch ----

def test_injeta_corretora_id_em_todas_as_tools_scoped():
    ag = AgenteAssistente(phone="+5500000000000", sender_kind="corretora",
                       sender_id=3, sender_nome="Pessoa3")
    for tool in TOOLS_SCOPED_TO_CORRETORA:
        out = ag._maybe_inject_corretora(tool, {})
        assert out.get("_for_corretora_id") == 3, f"{tool} não recebeu escopo da corretora"


def test_dona_nao_recebe_escopo_de_corretora():
    ag = AgenteAssistente(phone="+5500000000000", sender_kind="dona",
                       sender_id=1, sender_nome="Pessoa Pessoa9 Acme")
    for tool in TOOLS_SCOPED_TO_CORRETORA:
        out = ag._maybe_inject_corretora(tool, {})
        assert "_for_corretora_id" not in out, f"{tool} escopou a dona indevidamente"


# ---- Furo 2: header X-Painel-User propagado ----

class _FakeResp:
    status_code = 200

    def json(self):
        return {"ok": True}


def _capture_httpx(monkeypatch):
    captured: dict = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResp()

    monkeypatch.setattr("httpx.get", fake_get)
    return captured


def test_fetch_painel_corretora_usa_login_dela(monkeypatch):
    captured = _capture_httpx(monkeypatch)
    _fetch_painel("visao-geral", _for_corretora_id=3)  # id 3 → corretor
    assert captured["headers"]["X-Painel-User"] == "corretor"


def test_fetch_painel_dona_usa_dona(monkeypatch):
    captured = _capture_httpx(monkeypatch)
    _fetch_painel("visao-geral", _for_corretora_id=None)
    assert captured["headers"]["X-Painel-User"] == "dona"


def test_fetch_painel_corretora_desconhecida_nega_sem_chamar(monkeypatch):
    chamado = {"n": 0}

    def fake_get(*a, **k):
        chamado["n"] += 1
        return _FakeResp()

    monkeypatch.setattr("httpx.get", fake_get)
    out = _fetch_painel("visao-geral", _for_corretora_id=99)
    assert "_error" in out
    assert chamado["n"] == 0  # não escalou pra dona nem bateu no painel
