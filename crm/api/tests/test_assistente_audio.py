"""Testes do POST /painel/assistente/audio — a corretora fala em vez de digitar.

O endpoint só faz a ponte: transcreve (serviço existente, mockado aqui) e
manda a transcrição pro MESMO `_responder` do /painel/assistente. Por isso os
testes mockam os dois lados e checam contrato + tratamento de erro humano.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.api import assistente_copilota
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def _hdrs():
    # 'corretor' está em USUARIO_CORRETORA (painel.py) — usuário legítimo.
    return {"X-Painel-User": "corretor"}


def _files(conteudo: bytes, mime: str = "audio/webm"):
    return {"audio": ("gravacao.webm", conteudo, mime)}


_AUDIO_OK = b"\x1a\x45\xdf\xa3" + b"0" * 5000  # ~5KB, acima do mínimo


def test_audio_valido_transcreve_e_responde(client):
    with patch.object(
        assistente_copilota,
        "_responder",
        return_value={"resposta": "O João tá quente 🔥", "intent": "lead"},
    ) as mock_resp, patch(
        "app.services.transcricao_service.transcrever_audio",
        return_value={"texto": "como tá o lead João?", "modelo": "whisper-mac-large-v3", "erro": None},
    ) as mock_tr:
        r = client.post("/painel/assistente/audio", headers=_hdrs(), files=_files(_AUDIO_OK))

    assert r.status_code == 200
    body = r.json()
    # A transcrição volta pra tela como a mensagem enviada.
    assert body["transcricao"] == "como tá o lead João?"
    assert body["resposta"] == "O João tá quente 🔥"
    assert body["intent"] == "lead"
    mock_tr.assert_called_once()
    # A pergunta que chegou no cérebro é exatamente a transcrição.
    assert mock_resp.call_args.args[3] == "como tá o lead João?"


def test_audio_vazio_erro_tratado(client):
    with patch("app.services.transcricao_service.transcrever_audio") as mock_tr:
        r = client.post("/painel/assistente/audio", headers=_hdrs(), files=_files(b"\x00" * 10))

    assert r.status_code == 200
    body = r.json()
    assert body["transcricao"] is None
    assert "vazio" in body["resposta"].lower()
    mock_tr.assert_not_called()  # nem chega a gastar transcrição


def test_audio_grande_demais_erro_tratado(client):
    grande = b"0" * (assistente_copilota._AUDIO_MAX_BYTES + 1)
    with patch("app.services.transcricao_service.transcrever_audio") as mock_tr:
        r = client.post("/painel/assistente/audio", headers=_hdrs(), files=_files(grande))

    assert r.status_code == 200
    body = r.json()
    assert body["transcricao"] is None
    assert "2 minutos" in body["resposta"]
    mock_tr.assert_not_called()


def test_transcricao_falha_resposta_amigavel(client):
    """Whisper do Mac fora do ar → recado humano, nunca 500 nem erro técnico."""
    with patch.object(assistente_copilota, "_responder") as mock_resp, patch(
        "app.services.transcricao_service.transcrever_audio",
        return_value={"texto": None, "modelo": "whisper-mac-large-v3", "erro": "Connection refused"},
    ):
        r = client.post("/painel/assistente/audio", headers=_hdrs(), files=_files(_AUDIO_OK))

    assert r.status_code == 200
    body = r.json()
    assert body["transcricao"] is None
    assert body["resposta"] == assistente_copilota._AUDIO_ERRO_MSG
    assert "Connection refused" not in body["resposta"]
    mock_resp.assert_not_called()


def test_transcricao_levanta_nao_vira_500(client):
    with patch(
        "app.services.transcricao_service.transcrever_audio",
        side_effect=RuntimeError("boom"),
    ):
        r = client.post("/painel/assistente/audio", headers=_hdrs(), files=_files(_AUDIO_OK))

    assert r.status_code == 200
    assert r.json()["resposta"] == assistente_copilota._AUDIO_ERRO_MSG


def test_transcricao_em_branco_tratada_como_falha(client):
    """Whisper devolve texto vazio (silêncio) → mesmo recado, sem chamar o LLM."""
    with patch.object(assistente_copilota, "_responder") as mock_resp, patch(
        "app.services.transcricao_service.transcrever_audio",
        return_value={"texto": "   ", "modelo": "whisper-mac-large-v3", "erro": None},
    ):
        r = client.post("/painel/assistente/audio", headers=_hdrs(), files=_files(_AUDIO_OK))

    assert r.json()["resposta"] == assistente_copilota._AUDIO_ERRO_MSG
    mock_resp.assert_not_called()


def test_sem_header_usuario_bloqueia(client):
    r = client.post("/painel/assistente/audio", files=_files(_AUDIO_OK))
    assert r.status_code == 403
