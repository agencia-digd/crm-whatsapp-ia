"""Testes do POST /webhook/evolution/assistente/{secret} — Onda 2."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import settings
from app.db.session import SessionLocal
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_test_msgs():
    with SessionLocal() as s:
        s.execute(text("DELETE FROM conversations WHERE message_id LIKE 'wh-%'"))
        s.execute(text("DELETE FROM mensagens_whatsapp WHERE evolution_message_id LIKE 'wh-%'"))
        s.commit()
    yield
    with SessionLocal() as s:
        s.execute(text("DELETE FROM conversations WHERE message_id LIKE 'wh-%'"))
        s.execute(text("DELETE FROM mensagens_whatsapp WHERE evolution_message_id LIKE 'wh-%'"))
        s.commit()


def _payload_msg(
    phone_remote: str,
    text_msg: str,
    msg_id: str,
    from_me: bool = False,
    instance: str = "acme_corretora_a",
) -> dict:
    return {
        "event": "messages.upsert",
        "instance": instance,
        "data": {
            "key": {
                "remoteJid": f"{phone_remote}@s.whatsapp.net",
                "id": msg_id,
                "fromMe": from_me,
            },
            "message": {"conversation": text_msg},
        },
    }


def _fake_agent_run(self, message: str):
    return {"response_text": "Oi! (mock Assistente)", "tool_calls": []}


def test_secret_errado_404(client):
    r = client.post(
        "/webhook/evolution/assistente/nope",
        json=_payload_msg("+5500000000000", "oi", "wh-1"),
    )
    assert r.status_code == 404


def test_msg_de_pessoa_aciona_assistente_e_send(client):
    """Msg endereçada à Assistente (instância assistente, remetente=dona) enfileira no
    dispatcher assíncrono.

    O envio direto (`AgenteAssistente.run` + `EvolutionClient.send_text` chamados
    de dentro do handler) não existe mais — desde `enqueue_assistente_run`
    (app/services/assistente_dispatch.py), o handler só ENFILEIRA a mensagem
    (debounce 6s + typing keeper) e retorna 200 na hora; quem chama o agente e
    envia é o worker assíncrono do dispatcher, testado à parte. Mesmo padrão
    já usado em test_webhook_audio_dispara_transcricao_async neste arquivo
    (mock de `app.services.assistente_dispatch.enqueue_assistente_run`).
    """
    from types import SimpleNamespace as _SimpleNamespace

    secret = settings.evolution_webhook_secret
    captured: dict = {}

    def _fake_enqueue(**kwargs):
        captured.update(kwargs)

    with patch(
        "app.api.evolution_webhook.identify_sender",
        return_value=_SimpleNamespace(
            kind="dona", id=None, nome="Pessoa Pessoa9 Acme", papel="dona"
        ),
    ), patch(
        "app.services.assistente_dispatch.enqueue_assistente_run", _fake_enqueue
    ), patch(
        "app.api.evolution_webhook.EvolutionClient"
    ):
        r = client.post(
            f"/webhook/evolution/assistente/{secret}",
            # instância precisa ser a da Assistente (routing por destinatário,
            # ver evolution_webhook.py:170-181) — a Pessoa fala PRA Assistente
            # pela instância assistente, fromMe=False.
            json=_payload_msg("+5500000000000", "oi Beti", "wh-pessoa",
                              instance=settings.evolution_assistente_instance),
        )
    assert r.status_code == 200
    assert captured, "enqueue_assistente_run não foi chamado"
    assert captured["phone"] == "+5500000000000"
    assert captured["text"] == "oi Beti"
    assert captured["sender_kind"] == "dona"


def test_from_me_ignorado(client):
    """Echo (fromMe=true) endereçado a alguém que NÃO é o próprio número da
    Assistente não enfileira nada (ver dest_phone != assistente_phone em
    evolution_webhook.py:172-181)."""
    secret = settings.evolution_webhook_secret
    with patch("app.services.assistente_dispatch.enqueue_assistente_run") as mock_enqueue, \
         patch("app.api.evolution_webhook.EvolutionClient") as MockClient:
        r = client.post(
            f"/webhook/evolution/assistente/{secret}",
            json=_payload_msg("+5500000000000", "ecoo", "wh-fromme", from_me=True),
        )
        assert r.status_code == 200
        mock_enqueue.assert_not_called()
        MockClient.return_value.send_text.assert_not_called()


def test_numero_unknown_loga_e_nao_envia(client):
    secret = settings.evolution_webhook_secret
    with patch("app.api.evolution_webhook.EvolutionClient") as MockClient:
        r = client.post(
            f"/webhook/evolution/assistente/{secret}",
            json=_payload_msg("+5500000000000", "oi", "wh-unknown"),
        )
        assert r.status_code == 200
        MockClient.return_value.send_text.assert_not_called()


def test_payload_mal_formado_200_sem_erro(client):
    secret = settings.evolution_webhook_secret
    r = client.post(
        f"/webhook/evolution/assistente/{secret}",
        json={"event": "qualquer-coisa", "data": {}},
    )
    assert r.status_code == 200


def _payload_audio(
    phone_remote: str,
    msg_id: str,
    instance: str = "acme_corretor",
) -> dict:
    return {
        "event": "messages.upsert",
        "instance": instance,
        "data": {
            "key": {
                "remoteJid": f"{phone_remote}@s.whatsapp.net",
                "id": msg_id,
                "fromMe": False,
            },
            "message": {
                "audioMessage": {
                    "ptt": True,
                    "seconds": 8,
                    "mimetype": "audio/ogg; codecs=opus",
                }
            },
            "messageTimestamp": 1715000000,
        },
    }


def test_webhook_audio_dispara_transcricao_async(client):
    """Áudio novo → create_task(_transcrever_async(uuid)) é agendado."""
    secret = settings.evolution_webhook_secret
    fake_id = uuid.uuid4()

    with patch(
        "app.api.evolution_webhook.gravar_mensagem_evolution",
        return_value=fake_id,
    ), patch(
        "app.api.evolution_webhook.asyncio.create_task"
    ) as mock_create_task:
        r = client.post(
            f"/webhook/evolution/assistente/{secret}",
            json=_payload_audio("+5500000000000", "wh-audio-001"),
        )

    assert r.status_code == 200
    mock_create_task.assert_called_once()


def test_webhook_audio_falha_transcricao_nao_quebra_webhook(client):
    """Falha na transcrição async não impede resposta 200 do webhook."""
    secret = settings.evolution_webhook_secret
    fake_id = uuid.uuid4()

    async def _explode(_uid):
        raise RuntimeError("gemini caiu")

    with patch(
        "app.api.evolution_webhook.gravar_mensagem_evolution",
        return_value=fake_id,
    ), patch(
        "app.api.evolution_webhook._transcrever_async",
        side_effect=_explode,
    ):
        r = client.post(
            f"/webhook/evolution/assistente/{secret}",
            json=_payload_audio("+5500000000000", "wh-audio-002"),
        )

    assert r.status_code == 200


def test_webhook_audio_injeta_transcricao_real_nao_placeholder(client):
    """Regressão (review 2026-07-10 §2): áudio pra Assistente deve injetar a
    transcrição REAL do campo `MensagemWhatsapp.transcricao` — não o
    placeholder '[áudio recebido]'.

    Antes do fix o código lia `msg_row.transcricao_texto` (atributo inexistente),
    levantava AttributeError capturado por except genérico, e a Assistente recebia
    '[áudio recebido]' literal em vez da transcrição.
    """
    from types import SimpleNamespace
    from contextlib import contextmanager

    secret = settings.evolution_webhook_secret or "test-secret"
    fake_id = uuid.uuid4()

    # Linha da própria Assistente (evolution_assistente_instance) + fromMe=false →
    # dest_phone == assistente_phone → cai no bloco de transcrição síncrona.
    payload = _payload_audio(
        "+5500000000000", "wh-audio-real", instance=settings.evolution_assistente_instance
    )

    # Row com a transcrição REAL preenchida no atributo correto.
    fake_row = SimpleNamespace(transcricao="quero agendar visita amanhã às 10h")

    @contextmanager
    def _fake_session():
        sess = MagicMock()
        sess.execute.return_value.scalar_one_or_none.return_value = fake_row
        yield sess

    captured = {}

    def _fake_enqueue(**kwargs):
        captured.update(kwargs)

    with patch(
        "app.api.evolution_webhook.gravar_mensagem_evolution", return_value=fake_id
    ), patch(
        "app.api.evolution_webhook.is_duplicate", return_value=False
    ), patch(
        "app.api.evolution_webhook._assistente_silenced", return_value=False
    ), patch(
        "app.api.evolution_webhook.identify_sender",
        return_value=SimpleNamespace(
            kind="corretora", id=1, nome="Pessoa11", papel="corretora_solo"
        ),
    ), patch(
        "app.api.evolution_webhook.append_message"
    ), patch(
        "app.services.transcricao_service.transcrever_mensagem", return_value=True
    ), patch(
        "app.db.session.SessionLocal", _fake_session
    ), patch(
        "app.services.assistente_dispatch.enqueue_assistente_run", _fake_enqueue
    ), patch(
        "app.api.evolution_webhook.settings.evolution_webhook_secret", secret
    ):
        r = client.post(f"/webhook/evolution/assistente/{secret}", json=payload)

    assert r.status_code == 200
    assert captured, "enqueue_assistente_run não foi chamado"
    assert captured["text"] == "🎤 quero agendar visita amanhã às 10h"
    assert captured["text"] != "[áudio recebido]"
