"""
Teste de concorrência do orchestrator do recovery — correção Codex F5.

Cenário: DUAS mensagens DIFERENTES do mesmo lead chegam quase juntas e são
processadas em paralelo (duas threads). Sem serialização por lead, ambas leem
o mesmo estado (turns=N), ambas chamam o LLM e enviam resposta, e o último
commit sobrescreve o estado da outra -> um turno é PERDIDO (turns final = N+1
em vez de N+2).

Com o advisory lock por lead (bloqueante) do wrapper `process_incoming`, a 2a
mensagem espera a 1a terminar e lê o estado JA commitado, então:
  - turns final = N+2 (nenhum turno perdido)
  - as DUAS respostas da Corretor são persistidas no histórico

Requer DB real (TEST_DATABASE_URL). Sem ela, o teste é pulado.
"""
from __future__ import annotations

import os
import threading
import time
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.db.models import RecoveryConversation, RecoveryMessage
from app.recovery import orchestrator as orch
from app.recovery.conversation import State

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="teste de concorrência exige DB real (TEST_DATABASE_URL)",
)

_LEAD_ID = "concurrency_F5_lead"
_PHONE = "+5500000000000"


def _cleanup() -> None:
    with SessionLocal() as s:
        s.query(RecoveryMessage).filter(RecoveryMessage.lead_id == _LEAD_ID).delete()
        s.query(RecoveryConversation).filter(
            RecoveryConversation.lead_id == _LEAD_ID
        ).delete()
        s.commit()


def _seed_qualificando() -> None:
    """Cria a conversa já em QUALIFICANDO com turns=0 (lead já respondeu antes)."""
    with SessionLocal() as s:
        s.add(
            RecoveryConversation(
                lead_id=_LEAD_ID,
                lead_phone=_PHONE,
                lead_name="Fulano F5",
                state=State.QUALIFICANDO.value,
                turns=0,
            )
        )
        s.commit()


def test_duas_msgs_do_mesmo_lead_nao_perdem_turno(monkeypatch):
    _cleanup()
    _seed_qualificando()

    # Mocka LLM (com sleep pra alargar a janela de corrida) e o envio real.
    # O sleep garante que, SEM lock, as duas threads leriam o mesmo estado.
    def _slow_reply(name, history):
        time.sleep(0.4)
        return MagicMock(
            text="resposta da Corretor",
            qualified=False,
            ended=False,
            end_reason=None,
        )

    monkeypatch.setattr(orch, "generate_reply", _slow_reply)
    monkeypatch.setattr(orch, "_send_via_evolution", lambda phone, text: True)

    barrier = threading.Barrier(2)
    results: list = []
    errors: list = []

    def _worker(msg: str):
        try:
            barrier.wait()  # dispara as duas quase simultâneas
            res = orch.process_incoming(
                lead_phone=_PHONE,
                lead_name="Fulano F5",
                lead_id=_LEAD_ID,
                text=msg,
            )
            results.append(res)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=_worker, args=("primeira msg",))
    t2 = threading.Thread(target=_worker, args=("segunda msg",))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert not errors, f"threads levantaram exceção: {errors}"
    assert len(results) == 2

    with SessionLocal() as s:
        conv = s.scalar(
            select(RecoveryConversation).where(
                RecoveryConversation.lead_id == _LEAD_ID
            )
        )
        corretor_msgs = s.scalars(
            select(RecoveryMessage).where(
                RecoveryMessage.lead_id == _LEAD_ID,
                RecoveryMessage.role == "corretor",
            )
        ).all()
        lead_msgs = s.scalars(
            select(RecoveryMessage).where(
                RecoveryMessage.lead_id == _LEAD_ID,
                RecoveryMessage.role == "lead",
            )
        ).all()

    # A prova da serialização: cada on_corretor_sent incrementa turns. Sem lock, as
    # duas threads leem turns=0 e gravam turns=1 (turno perdido). Com lock,
    # a 2a lê turns=1 já commitado -> turns final = 2.
    assert conv is not None
    assert conv.turns == 2, f"turno perdido -- turns={conv.turns} (esperado 2)"
    # As duas respostas da Corretor foram persistidas (nenhuma sobrescrita).
    assert len(corretor_msgs) == 2, f"esperava 2 msgs da Corretor, veio {len(corretor_msgs)}"
    # As duas mensagens do lead também.
    assert len(lead_msgs) == 2, f"esperava 2 msgs do lead, veio {len(lead_msgs)}"

    _cleanup()
