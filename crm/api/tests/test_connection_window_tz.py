"""Teste do fuso em _apply_connection_window (Codex #10).

Um evolution_connected_since já com fuso (-03:00) deve ser CONVERTIDO para UTC,
não sobrescrito. Antes o replace(tzinfo=utc) incondicional deslocava o marco em
3h e deixava passar mensagens anteriores à conexão real.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.inspector.pipeline import _apply_connection_window


def test_connected_since_com_fuso_e_convertido_nao_sobrescrito():
    # conexão às 12:00 no fuso -03:00  ==  15:00 UTC
    connected = datetime(2026, 7, 1, 12, 0, tzinfo=timezone(timedelta(hours=-3)))
    msgs = [
        {"at": "2026-07-01T14:30:00+00:00", "id": "antes"},   # 14:30 UTC — antes da conexão
        {"at": "2026-07-01T15:30:00+00:00", "id": "depois"},  # 15:30 UTC — depois
    ]
    ids = [m["id"] for m in _apply_connection_window(msgs, connected)]
    assert "depois" in ids
    # com o bug (replace → marco vira 12:00 UTC) 'antes' passaria indevidamente
    assert "antes" not in ids


def test_connected_since_naive_assume_utc():
    connected = datetime(2026, 7, 1, 15, 0)  # naive → tratado como UTC
    msgs = [
        {"at": "2026-07-01T14:30:00+00:00", "id": "antes"},
        {"at": "2026-07-01T15:30:00+00:00", "id": "depois"},
    ]
    ids = [m["id"] for m in _apply_connection_window(msgs, connected)]
    assert ids == ["depois"]
