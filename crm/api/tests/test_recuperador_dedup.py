"""Teste do dedup do webhook do recuperador (Codex #5).

Prova que uma REENTREGA do mesmo update do webhook não passa de novo pelo fluxo
da Corretor. Antes o dedup consultava a tabela `conversations` (que o recovery nunca
popula — ele usa recovery_messages), então toda reentrega re-rodava a Corretor e o
lead podia receber resposta repetida. Agora o dedup usa o retorno da gravação
idempotente em mensagens_whatsapp (UUID=nova, None=duplicata).
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import app.api.evolution_webhook as wh
from app.config import settings
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def _payload(msg_id: str) -> dict:
    return {
        "event": "messages.upsert",
        "instance": "recuperador",
        "data": {
            "key": {
                "remoteJid": "+5500000000000@s.whatsapp.net",
                "id": msg_id,
                "fromMe": False,
            },
            "message": {"conversation": "oi, ainda tenho interesse"},
        },
    }


def test_reentrega_do_webhook_recuperador_nao_reroda_corretor(client, monkeypatch):
    monkeypatch.setattr(settings, "evolution_webhook_secret", "sec")
    monkeypatch.setattr(settings, "evolution_recuperador_instance", "recuperador")

    # gravar em mensagens_whatsapp: UUID (nova) na 1ª entrega, None (dup) na 2ª
    monkeypatch.setattr(
        wh, "gravar_mensagem_evolution", MagicMock(side_effect=[uuid.uuid4(), None])
    )

    # se passar do dedup, o webhook chama find_by_phone → contamos as passagens
    passou = {"n": 0}

    def _find(*a, **k):
        passou["n"] += 1
        return None  # conv None → webhook encerra sem acionar a Corretor

    monkeypatch.setattr(wh.recovery_repo, "find_by_phone", _find)

    corretor = MagicMock()
    monkeypatch.setattr(wh, "recovery_process", corretor)

    p = _payload("rec-dup-1")

    r1 = client.post("/webhook/evolution/recuperador/sec", json=p)
    assert r1.status_code == 200
    assert passou["n"] == 1  # 1ª entrega passou do dedup

    r2 = client.post("/webhook/evolution/recuperador/sec", json=p)
    assert r2.status_code == 200
    assert passou["n"] == 1  # reentrega foi DEDUPLICADA (não passou de novo)

    corretor.assert_not_called()
