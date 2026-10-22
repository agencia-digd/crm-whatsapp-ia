"""State machine pura — sem LLM, sem IO."""
from datetime import datetime, timedelta, timezone

import pytest

from app.recovery.conversation import (
    DROP_AFTER_REENGAGE,
    REENGAGE_AFTER,
    Conversation,
    State,
    is_terminal,
)


def _utc(h: int = 12) -> datetime:
    return datetime(2026, 5, 21, h, 0, tzinfo=timezone.utc)


def test_aguardando_to_qualificando_on_first_lead_msg() -> None:
    c = Conversation(lead_id="L1")
    assert c.state == State.AGUARDANDO
    c.on_lead_message(_utc(10))
    assert c.state == State.QUALIFICANDO


def test_corretor_sent_normal_keeps_qualificando() -> None:
    c = Conversation(lead_id="L1", state=State.QUALIFICANDO)
    c.on_corretor_sent(_utc(11), qualified=False, ended=False, reason=None)
    assert c.state == State.QUALIFICANDO
    assert c.turns == 1


def test_qualified_transitions_to_qualificado_terminal() -> None:
    c = Conversation(lead_id="L1", state=State.QUALIFICANDO)
    c.on_corretor_sent(_utc(11), qualified=True, ended=False, reason=None)
    assert c.state == State.QUALIFICADO
    assert is_terminal(c.state)


def test_ended_aluguel_transitions_to_perdido() -> None:
    c = Conversation(lead_id="L1", state=State.QUALIFICANDO)
    c.on_corretor_sent(_utc(11), qualified=False, ended=True, reason="aluguel")
    assert c.state == State.PERDIDO
    assert c.end_reason == "aluguel"
    assert is_terminal(c.state)


def test_ended_opt_out() -> None:
    c = Conversation(lead_id="L1", state=State.QUALIFICANDO)
    c.on_corretor_sent(_utc(11), qualified=False, ended=True, reason="opt_out")
    assert c.end_reason == "opt_out"


def test_tick_triggers_reengage_after_silence() -> None:
    c = Conversation(lead_id="L1", state=State.QUALIFICANDO)
    c.last_lead_msg_at = _utc(0)
    now = _utc(0) + REENGAGE_AFTER + timedelta(minutes=1)
    assert c.on_tick(now) == "reengage"


def test_tick_noop_when_within_window() -> None:
    c = Conversation(lead_id="L1", state=State.QUALIFICANDO)
    c.last_lead_msg_at = _utc(0)
    now = _utc(0) + timedelta(hours=2)
    assert c.on_tick(now) == "noop"


def test_tick_drops_after_reengage_window() -> None:
    c = Conversation(lead_id="L1", state=State.REENGAJADO, reengage_done=True)
    c.last_lead_msg_at = _utc(0)
    now = _utc(0) + DROP_AFTER_REENGAGE + timedelta(minutes=1)
    assert c.on_tick(now) == "drop"


def test_tick_noop_after_qualified() -> None:
    c = Conversation(lead_id="L1", state=State.QUALIFICADO)
    c.last_lead_msg_at = _utc(0)
    assert c.on_tick(_utc(0) + timedelta(days=7)) == "noop"


def test_reengage_only_fires_once() -> None:
    c = Conversation(lead_id="L1", state=State.QUALIFICANDO)
    c.last_lead_msg_at = _utc(0)
    now = _utc(0) + REENGAGE_AFTER + timedelta(minutes=1)
    assert c.on_tick(now) == "reengage"
    c.mark_reengaged(now)
    # Mesmo tick depois: estado já mudou, não deve reengajar de novo
    assert c.on_tick(now + timedelta(minutes=1)) == "noop"


def test_lead_reply_after_reengage_returns_to_qualificando() -> None:
    c = Conversation(lead_id="L1", state=State.REENGAJADO, reengage_done=True)
    c.on_lead_message(_utc(15))
    assert c.state == State.QUALIFICANDO
