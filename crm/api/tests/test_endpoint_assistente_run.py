"""Testes do POST /assistente/run — Onda 2.

Telefones vêm das constantes de `tests/conftest.py` (fonte única do seed) —
ver a nota em test_sender_lookup.py sobre o literal do número da Pessoa12.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import settings
from app.db.session import SessionLocal
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


@pytest.fixture(autouse=True)
def _clean_test_msgs():
    """Limpeza já escopada por marcador: todo `message_id` semeado aqui começa
    com 'test-'. Mensagem real da Assistente nunca tem esse prefixo, então o DELETE
    não alcança conversa de produção nem se o banco estivesse errado.
    """
    _apagar_do_teste()
    yield
    _apagar_do_teste()


def _apagar_do_teste() -> None:
    with SessionLocal() as s:
        s.execute(text("DELETE FROM conversations WHERE message_id LIKE 'test-%'"))
        s.commit()


def _hdrs():
    return {"X-Internal-Token": settings.copiloto_internal_token}


def _fake_run(self, message: str):
    return {"response_text": "Sou a Assistente. (mock)", "tool_calls": []}


def test_pessoa_dona_responde(client):
    with patch("app.api.assistente.AgenteAssistente.run", new=_fake_run):
        r = client.post(
            "/assistente/run",
            headers=_hdrs(),
            json={"from_phone": TEL_DONA, "text": "oi", "message_id": "test-pessoa-1"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["sender"]["kind"] == "dona"
    assert body["sender"]["nome"] == "Pessoa Pessoa9 Acme"


def test_corretora_a_corretora_responde(client):
    with patch("app.api.assistente.AgenteAssistente.run", new=_fake_run):
        r = client.post(
            "/assistente/run",
            headers=_hdrs(),
            json={"from_phone": TEL_CORRETORA_A, "text": "oi", "message_id": "test-corretor-1"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["sender"]["kind"] == "corretora"
    assert body["sender"]["nome"] == "Pessoa3"


def test_daniela_removida_404(client):
    """Onda P1: Dona saiu de internos_acme → 404 not_mine."""
    with patch("app.api.assistente.AgenteAssistente.run", new=_fake_run):
        r = client.post(
            "/assistente/run",
            headers=_hdrs(),
            json={"from_phone": TEL_DANIELA_REMOVIDA, "text": "oi", "message_id": "test-dona-1"},
        )
    assert r.status_code == 404


def test_phone_aleatorio_404(client):
    with patch("app.api.assistente.AgenteAssistente.run", new=_fake_run):
        r = client.post(
            "/assistente/run",
            headers=_hdrs(),
            json={"from_phone": TEL_DESCONHECIDO, "text": "oi"},
        )
    assert r.status_code == 404


def test_sem_token_401(client):
    r = client.post(
        "/assistente/run",
        json={"from_phone": TEL_DONA, "text": "oi"},
    )
    assert r.status_code == 401


def test_phone_invalido_400(client):
    r = client.post(
        "/assistente/run",
        headers=_hdrs(),
        json={"from_phone": "abc", "text": "oi"},
    )
    assert r.status_code == 400


def test_message_id_duplicado(client):
    with patch("app.api.assistente.AgenteAssistente.run", new=_fake_run):
        r1 = client.post(
            "/assistente/run",
            headers=_hdrs(),
            json={"from_phone": TEL_DONA, "text": "oi", "message_id": "test-dup"},
        )
        assert r1.status_code == 200
        r2 = client.post(
            "/assistente/run",
            headers=_hdrs(),
            json={"from_phone": TEL_DONA, "text": "oi", "message_id": "test-dup"},
        )
    assert r2.status_code == 200
    assert r2.json()["status"] == "duplicate"


def test_timeout_retorna_status_timeout(client):
    import time

    def slow_run(self, message: str):
        time.sleep(30)
        return {"response_text": "tarde demais", "tool_calls": []}

    with patch("app.api.assistente.AgenteAssistente.run", new=slow_run):
        with patch("app.api.assistente.AGENT_TIMEOUT_S", 0.5):
            r = client.post(
                "/assistente/run",
                headers=_hdrs(),
                json={"from_phone": TEL_DONA, "text": "oi", "message_id": "test-timeout"},
            )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "timeout"
    assert "Beti" in body["response_text"]


def test_late_response_persiste_e_despacha_1x(client):
    """Codex F12: agent termina DEPOIS do timeout HTTP → a resposta real é
    persistida e despachada exatamente 1x via _deliver_late_response.
    """
    import threading
    import time

    release = threading.Event()
    late_text = "Resposta que ficou pronta tarde. (Assistente)"

    def slow_run(self, message: str):
        # Segura até liberarmos (garante que o timeout HTTP dispara primeiro)
        release.wait(timeout=5)
        return {"response_text": late_text, "tool_calls": []}

    delivered: list[str] = []

    def _fake_deliver(phone: str, text: str) -> None:
        delivered.append(text)

    with patch("app.api.assistente.AgenteAssistente.run", new=slow_run), \
         patch("app.api.assistente.AGENT_TIMEOUT_S", 0.3), \
         patch("app.api.assistente._deliver_late_response", new=_fake_deliver):
        r = client.post(
            "/assistente/run",
            headers=_hdrs(),
            json={"from_phone": TEL_DONA, "text": "oi", "message_id": "test-late-1"},
        )
        # HTTP já respondeu timeout; agora libera o agent pra terminar
        assert r.status_code == 200
        assert r.json()["status"] == "timeout"
        release.set()
        # Espera o callback pós-timeout rodar (best-effort, alguns ms)
        for _ in range(50):
            if delivered:
                break
            time.sleep(0.05)

    # (a) entregou 1x com o texto REAL do agent (não o placeholder)
    assert delivered == [late_text]


def test_guard_impede_segunda_entrega(client):
    """O _DeliveryGuard só deixa reivindicar a entrega uma vez."""
    from app.api.assistente import _DeliveryGuard

    guard = _DeliveryGuard()
    assert guard.claim() is True
    assert guard.claim() is False
    assert guard.claim() is False


def test_caso_rapido_ok_sem_envio_evolution(client):
    """Caso normal (agent termina antes do timeout): status=ok, texto no corpo,
    e NENHUM envio via _deliver_late_response (o /run rápido não despacha).
    """
    delivered: list[str] = []

    def _fake_deliver(phone: str, text: str) -> None:
        delivered.append(text)

    with patch("app.api.assistente.AgenteAssistente.run", new=_fake_run), \
         patch("app.api.assistente._deliver_late_response", new=_fake_deliver):
        r = client.post(
            "/assistente/run",
            headers=_hdrs(),
            json={"from_phone": TEL_DONA, "text": "oi", "message_id": "test-fast-1"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["response_text"] == "Sou a Assistente. (mock)"
    assert delivered == []


@pytest.mark.skip(reason="Pessoa5 offline — quando reconectar, remove")
def test_corretora_c_corretora(client):
    pass
