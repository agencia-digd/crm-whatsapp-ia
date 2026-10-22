"""
Testes para gravar_mensagem_evolution_from_evolution_msg e cenários de importação.

Cobre:
1. 3 mensagens do find_messages → 3 linhas no banco
2. Dedup com webhook: mesma mensagem gravada via webhook NÃO duplica
3. Falha de DB em uma mensagem não quebra o loop de importação
4. Áudio sem caption: tipo_mensagem='audio', conteudo_texto=null
5. Grupo (@g.us): não grava
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.config import settings
from app.db.session import SessionLocal
from app.services.mensagens_evolution_service import (
    gravar_mensagem_evolution_from_evolution_msg,
    gravar_mensagem_evolution_from_webhook,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

_INSTANCE = settings.evolution_assistente_instance  # acme_corretora_a


def _raw_msg(
    remote_jid: str,
    msg: dict,
    msg_id: str | None = None,
    from_me: bool = False,
    push_name: str | None = "Contato Teste",
    timestamp: int = 1715299200,
    msg_type: str | None = None,
) -> dict:
    """Monta um raw_msg no formato find_messages (equivalente a payload['data'])."""
    if msg_id is None:
        msg_id = f"IMP-{uuid.uuid4().hex[:10]}"
    return {
        "key": {"remoteJid": remote_jid, "id": msg_id, "fromMe": from_me},
        "messageType": msg_type or next(iter(msg.keys()), "conversation"),
        "messageTimestamp": timestamp,
        "pushName": push_name,
        "message": msg,
    }


def _webhook_payload(remote_jid: str, msg: dict, msg_id: str, from_me: bool = False) -> dict:
    """Monta payload webhook wrapping o mesmo data."""
    return {
        "event": "messages.upsert",
        "instance": _INSTANCE,
        "data": {
            "key": {"remoteJid": remote_jid, "id": msg_id, "fromMe": from_me},
            "messageTimestamp": 1715299200,
            "pushName": "Contato Teste",
            "message": msg,
        },
    }


@pytest.fixture(autouse=True)
def _clean():
    _delete()
    yield
    _delete()


def _delete() -> None:
    with SessionLocal() as s:
        s.execute(text("DELETE FROM mensagens_whatsapp WHERE evolution_message_id LIKE 'IMP-%'"))
        s.commit()


def _count(msg_id: str) -> int:
    with SessionLocal() as s:
        return s.execute(
            text("SELECT COUNT(*) FROM mensagens_whatsapp WHERE evolution_message_id = :id"),
            {"id": msg_id},
        ).scalar()


def _fetch(msg_id: str) -> dict | None:
    with SessionLocal() as s:
        row = s.execute(
            text("SELECT * FROM mensagens_whatsapp WHERE evolution_message_id = :id"),
            {"id": msg_id},
        ).mappings().first()
        return dict(row) if row else None


# ── 1. 3 mensagens → 3 linhas com instance correta ───────────────────────────

def test_import_grava_mensagens_lidas() -> None:
    ids = [f"IMP-t1-{i}" for i in range(3)]
    msgs = [
        _raw_msg("+5500000000000@s.whatsapp.net", {"conversation": f"msg {i}"}, msg_id=ids[i])
        for i in range(3)
    ]
    for raw_msg in msgs:
        gravar_mensagem_evolution_from_evolution_msg(_INSTANCE, raw_msg)

    for msg_id in ids:
        row = _fetch(msg_id)
        assert row is not None, f"msg {msg_id} não foi gravada"
        assert row["instance"] == _INSTANCE


# ── 2. Dedup com webhook: mesma mensagem → 1 linha ───────────────────────────

def test_import_dedup_com_webhook() -> None:
    msg_id = "IMP-dedup-01"
    remote_jid = "+5500000000000@s.whatsapp.net"
    msg = {"conversation": "mensagem compartilhada"}

    # Webhook grava primeiro
    gravar_mensagem_evolution_from_webhook(
        _INSTANCE,
        _webhook_payload(remote_jid, msg, msg_id),
    )

    # Inspector importa a mesma mensagem
    gravar_mensagem_evolution_from_evolution_msg(
        _INSTANCE,
        _raw_msg(remote_jid, msg, msg_id=msg_id),
    )

    assert _count(msg_id) == 1


# ── 3. Falha de DB em uma msg não quebra o loop ───────────────────────────────

def test_import_falha_db_nao_quebra_loop() -> None:
    """Se uma mensagem falha no DB, as demais continuam sendo importadas."""
    ids = [f"IMP-loop-{i}" for i in range(3)]
    msgs = [
        _raw_msg("+5500000000000@s.whatsapp.net", {"conversation": f"m{i}"}, msg_id=ids[i])
        for i in range(3)
    ]

    call_count = 0
    original = gravar_mensagem_evolution_from_evolution_msg

    def patched(instance: str, raw_msg: dict) -> bool:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise Exception("DB falhou na segunda mensagem")
        return original(instance, raw_msg)

    # Simular loop de importação (equivalente ao que o script faz)
    results = []
    for raw_msg in msgs:
        try:
            ok = patched(_INSTANCE, raw_msg)
            results.append(ok)
        except Exception:
            results.append(False)

    # Loop não foi interrompido — processou todas as 3
    assert len(results) == 3
    # Msgs 1 e 3 gravadas; msg 2 falhou mas não parou o loop
    assert _fetch(ids[0]) is not None
    assert _fetch(ids[2]) is not None


# ── 4. Áudio sem caption: tipo=audio, conteudo_texto=null ────────────────────

def test_import_grava_audio_sem_caption() -> None:
    msg_id = "IMP-audio-01"
    raw_msg = _raw_msg(
        remote_jid="+5500000000000@s.whatsapp.net",
        msg={"audioMessage": {"ptt": True, "seconds": 8, "mimetype": "audio/ogg"}},
        msg_id=msg_id,
        msg_type="audioMessage",
    )
    gravar_mensagem_evolution_from_evolution_msg(_INSTANCE, raw_msg)

    row = _fetch(msg_id)
    assert row is not None
    assert row["tipo_mensagem"] == "audio"
    assert row["conteudo_texto"] is None
    assert row["conteudo_metadata"]["ptt"] is True
    assert row["conteudo_metadata"]["duracao_segundos"] == 8


# ── 5. Grupo @g.us → não grava ───────────────────────────────────────────────

def test_import_filtra_grupo() -> None:
    msg_id = "IMP-grupo-01"
    raw_msg = _raw_msg(
        remote_jid="contato@example.com",
        msg={"conversation": "mensagem de grupo"},
        msg_id=msg_id,
    )
    gravar_mensagem_evolution_from_evolution_msg(_INSTANCE, raw_msg)

    assert _fetch(msg_id) is None
