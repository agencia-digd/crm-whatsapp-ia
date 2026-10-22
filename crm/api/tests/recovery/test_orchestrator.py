"""
Testes do orchestrator do recovery — sem rede, sem DB (tudo mockado).

Foco na correção Codex #6: se HAVIA texto de resposta da Corretor e o envio real
(Evolution) falhou, o pipeline NÃO pode persistir a mensagem nem transicionar
o estado/finalizar no Kommo — senão o histórico afirma uma mensagem que o lead
nunca recebeu ("conversa fantasma").
"""
from __future__ import annotations

from unittest.mock import MagicMock

from app.recovery import orchestrator as orch


def _base_mocks(monkeypatch, *, send_ok: bool):
    """Mocka DB/repo/LLM/envio. Retorna a lista de roles passados a append_message."""
    monkeypatch.setattr(orch, "SessionLocal", lambda: MagicMock())
    # o wrapper público adquire um advisory lock por lead (engine.connect) antes
    # de delegar — mockado aqui pra manter estes testes sem DB.
    monkeypatch.setattr(orch, "_acquire_lead_lock", lambda lead_id: MagicMock())
    monkeypatch.setattr(orch, "_release_lead_lock", lambda conn, lead_id: None)

    conv_row = MagicMock(state="aguardando")
    monkeypatch.setattr(orch.repo, "get_or_create", lambda *a, **k: conv_row)
    monkeypatch.setattr(orch, "is_terminal", lambda st: False)

    conv = MagicMock()
    conv.state = orch.State.QUALIFICANDO  # não-terminal → não finaliza no Kommo
    monkeypatch.setattr(orch.repo, "to_domain", lambda row: conv)
    monkeypatch.setattr(orch.repo, "load_history", lambda *a, **k: [])

    roles: list[str] = []
    monkeypatch.setattr(
        orch.repo, "append_message",
        lambda session, lead_id, role, text: roles.append(role),
    )
    monkeypatch.setattr(orch.repo, "save_state", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_finalize_in_kommo", lambda *a, **k: None)

    reply = MagicMock(text="Oi, ainda tá procurando imóvel?",
                      qualified=False, ended=False, end_reason=None)
    monkeypatch.setattr(orch, "generate_reply", lambda name, hist: reply)
    monkeypatch.setattr(orch, "_send_via_evolution", lambda phone, text: send_ok)
    return roles


def test_envio_falha_nao_persiste_msg_da_corretor(monkeypatch):
    roles = _base_mocks(monkeypatch, send_ok=False)

    result = orch.process_incoming(
        lead_phone="+5500000000000", lead_name="João", lead_id="L1", text="oi",
    )

    assert result.sent is False
    assert result.skipped_reason == "send_failed"
    # a msg do LEAD é persistida antes do envio; a da CORRETOR NÃO (envio falhou)
    assert "lead" in roles
    assert "corretor" not in roles


def test_envio_ok_persiste_msg_da_corretor(monkeypatch):
    roles = _base_mocks(monkeypatch, send_ok=True)

    result = orch.process_incoming(
        lead_phone="+5500000000000", lead_name="João", lead_id="L1", text="oi",
    )

    assert result.sent is True
    assert "corretor" in roles
