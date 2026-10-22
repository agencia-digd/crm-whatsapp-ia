"""Dispatcher assíncrono pra Assistente — debounce + typing + send.

Em vez do webhook chamar `agent.run` direto (bloqueando 30-60s),
buffera mensagens por phone e processa em thread separada após
janela de debounce. Resolve 3 problemas:

1. Webhook responde 200 imediato pro Evolution (best practice)
2. Múltiplas mensagens rápidas viram 1 run com contexto agrupado
3. Permite typing indicator sem bloquear handler HTTP

State em memória — sobrevive enquanto uvicorn estiver vivo.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import structlog

from app.agents.assistente.agent import AgenteAssistente
from app.config import settings
from app.integrations.evolution_client import EvolutionClient
from app.services.conversations import append_message, load_history
from app.services.typing_keeper import TypingKeeper

log = structlog.get_logger()

DEBOUNCE_SECONDS = 6.0


@dataclass
class _PhoneState:
    messages: list[str] = field(default_factory=list)
    sender_kind: str = ""
    sender_id: int | None = None
    sender_nome: str | None = None
    sender_papel: str | None = None
    last_received_at: float = 0.0
    running: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


_states: dict[str, _PhoneState] = {}
_states_lock = threading.Lock()


def enqueue_assistente_run(
    phone: str,
    text: str,
    sender_kind: str,
    sender_id: int | None,
    sender_nome: str | None,
    sender_papel: str | None,
) -> None:
    """Adiciona msg ao buffer do phone e garante que um worker tá rodando."""
    with _states_lock:
        state = _states.get(phone)
        if state is None:
            state = _PhoneState()
            _states[phone] = state

    with state.lock:
        state.messages.append(text)
        state.sender_kind = sender_kind
        state.sender_id = sender_id
        state.sender_nome = sender_nome
        state.sender_papel = sender_papel
        state.last_received_at = time.monotonic()
        already_running = state.running
        if not already_running:
            state.running = True

    if already_running:
        log.info("assistente_dispatch_enqueued_to_running",
                 phone_last4=phone[-4:], buffer_size=len(state.messages))
        return

    t = threading.Thread(target=_run_worker, args=(phone, state), daemon=True,
                         name=f"assistente-{phone[-4:]}")
    t.start()


def _run_worker(phone: str, state: _PhoneState) -> None:
    """Worker daemon — debounce, run agent, send response. Roda até buffer vazio."""
    try:
        while True:
            # Debounce: espera até DEBOUNCE_SECONDS de silêncio
            while True:
                with state.lock:
                    silence = time.monotonic() - state.last_received_at
                    if silence >= DEBOUNCE_SECONDS:
                        # snapshot e limpa buffer
                        batch = list(state.messages)
                        state.messages.clear()
                        break
                time.sleep(min(DEBOUNCE_SECONDS - silence + 0.1, 2.0))

            if not batch:
                # Não deveria, mas safety
                break

            agrupado = "\n".join(batch) if len(batch) > 1 else batch[0]
            log.info("assistente_dispatch_processing", phone_last4=phone[-4:],
                     batch_size=len(batch), chars=len(agrupado))

            _process_one(phone, state, agrupado)

            # Se chegou msg nova durante o processamento → outro round
            with state.lock:
                if not state.messages:
                    state.running = False
                    return
            # Senão volta pro topo pra novo debounce
    except Exception as e:
        log.error("assistente_dispatch_worker_crash", phone_last4=phone[-4:], erro=str(e))
        with state.lock:
            state.running = False


def _process_one(phone: str, state: _PhoneState, text: str) -> None:
    """Roda o AgenteAssistente + typing + send. Best-effort, nunca levanta."""
    history = load_history(phone)
    agent = AgenteAssistente(
        phone=phone,
        sender_kind=state.sender_kind,
        sender_id=state.sender_id,
        sender_nome=state.sender_nome,
        sender_papel=state.sender_papel,
        history=history,
    )

    client = EvolutionClient()
    primeiro_nome = (state.sender_nome or "").split()[0] if state.sender_nome else None
    ack_text = (
        f"Ainda tô aqui, {primeiro_nome} — só mais um instantinho 👀"
        if primeiro_nome else "Ainda tô aqui — só mais um instantinho 👀"
    )

    def _ack_sender(t: str) -> None:
        if settings.dry_run:
            return
        try:
            client.send_text(instance_name=settings.evolution_assistente_instance,
                             to_number=phone, text=t)
        except Exception as e:
            log.warning("assistente_dispatch_ack_send_falhou", erro=str(e)[:80])

    keeper = TypingKeeper(
        instance=settings.evolution_assistente_instance,
        to_number=phone,
        ack_after_s=14.0,
        ack_sender=_ack_sender,
        ack_text=ack_text,
    )
    keeper.start()

    try:
        try:
            result = agent.run(text)
        except Exception as e:
            log.error("assistente_dispatch_agent_error", phone_last4=phone[-4:], erro=str(e))
            # Fallback humano em vez de silêncio
            result = {
                "response_text": (
                    f"Deu um pau técnico aqui agora, {primeiro_nome or ''}. "
                    "Tenta me mandar de novo daqui a pouco?".strip()
                ),
                "tool_calls": [],
            }
    finally:
        keeper.stop()

    response_text = result.get("response_text", "")
    if not response_text:
        return

    append_message(
        phone=phone, sender_kind="assistant", sender_id=None,
        message_role="assistant", text=response_text, message_id=None,
    )

    if settings.dry_run:
        log.info("assistente_dispatch_dry_run", phone_last4=phone[-4:],
                 chars=len(response_text))
        return

    try:
        client.send_text(instance_name=settings.evolution_assistente_instance,
                         to_number=phone, text=response_text)
    except Exception as e:
        log.error("assistente_dispatch_send_error", phone_last4=phone[-4:], erro=str(e))
