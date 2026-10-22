"""Testes do append/load/dedup de conversations — Onda 2."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.session import SessionLocal
from app.services.conversations import append_message, is_duplicate, load_history


@pytest.fixture(autouse=True)
def _clean_conversations():
    with SessionLocal() as s:
        s.execute(text("DELETE FROM conversations WHERE phone LIKE '999%'"))
        s.commit()
    yield
    with SessionLocal() as s:
        s.execute(text("DELETE FROM conversations WHERE phone LIKE '999%'"))
        s.commit()


def test_append_and_load_caps_at_max():
    phone = "9991111111111"
    for i in range(25):
        append_message(phone, "corretora", 1, "user", f"msg-{i}")
    history = load_history(phone, max_messages=20, max_age_hours=24)
    assert len(history) == 20
    # cronológico antigo→novo, então o último deve ser o "msg-24"
    assert history[-1]["text"] == "msg-24"


def test_load_history_respeita_janela_horas():
    phone = "9992222222222"
    # Insere uma msg "antiga" (mexer direto no DB pra forjar created_at)
    with SessionLocal() as s:
        s.execute(
            text(
                "INSERT INTO conversations (phone, sender_kind, message_role, "
                "text, created_at) VALUES (:p, 'corretora', 'user', "
                "'antiga', :t)"
            ),
            {"p": phone, "t": datetime.now(timezone.utc) - timedelta(hours=7)},
        )
        s.commit()
    append_message(phone, "corretora", 1, "user", "recente")
    h = load_history(phone, max_messages=10, max_age_hours=6)
    texts = [m["text"] for m in h]
    assert "recente" in texts
    assert "antiga" not in texts


def test_dedup_message_id():
    phone = "9993333333333"
    append_message(phone, "corretora", 1, "user", "primeira", message_id="dup-1")
    assert is_duplicate("dup-1") is True
    assert is_duplicate("nunca-existiu") is False
    with pytest.raises(IntegrityError):
        append_message(phone, "corretora", 1, "user", "segunda", message_id="dup-1")
