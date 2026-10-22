"""Mantém typing indicator ("digitando...") ativo via Evolution durante run longo.

Uso:
    keeper = TypingKeeper(instance="assistente", to_number=phone)
    keeper.start()
    try:
        result = agent.run(text)
    finally:
        keeper.stop()

Internamente: thread daemon que envia `sendPresence/composing` a cada
~20s — a Evolution renova o sinal pro WhatsApp; sem refresh ele expira
em ~5s. Best-effort: erros são logados, nunca levantados.

Também suporta `ack_after_seconds`: se passar N segundos e o keeper
ainda estiver vivo, envia uma msg de texto curta avisando que ainda
tá processando ("ainda tô aqui, [nome] — só mais um instantinho 👀").
Só envia uma vez por ciclo.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

import structlog

from app.integrations.evolution_client import EvolutionClient

log = structlog.get_logger()


class TypingKeeper:
    REFRESH_INTERVAL_S = 20.0
    ACK_AFTER_S_DEFAULT = 14.0

    def __init__(
        self,
        instance: str,
        to_number: str,
        ack_after_s: float | None = None,
        ack_sender: Callable[[str], None] | None = None,
        ack_text: str = "Ainda tô aqui, processando 👀",
    ) -> None:
        self.instance = instance
        self.to_number = to_number
        self.ack_after_s = ack_after_s if ack_after_s is not None else self.ACK_AFTER_S_DEFAULT
        self.ack_sender = ack_sender
        self.ack_text = ack_text
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client = EvolutionClient()
        self._start_ts: float = 0.0
        self._ack_sent = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._start_ts = time.monotonic()
        self._stop.clear()
        self._ack_sent = False
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"typing-{self.to_number[-4:]}")
        self._thread.start()
        # Primeiro envio imediato — não espera o primeiro tick
        self._send_presence_once()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        # Manda "paused" pra limpar o indicator do lado do cliente
        try:
            self._client.send_presence(self.instance, self.to_number, presence="paused", delay_ms=0)
        except Exception:
            pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._stop.wait(timeout=self.REFRESH_INTERVAL_S):
                break
            self._send_presence_once()
            # Ack textual após N segundos
            elapsed = time.monotonic() - self._start_ts
            if (
                not self._ack_sent
                and self.ack_after_s > 0
                and elapsed >= self.ack_after_s
                and self.ack_sender is not None
            ):
                try:
                    self.ack_sender(self.ack_text)
                    self._ack_sent = True
                    log.info("typing_keeper_ack_sent", to_last4=self.to_number[-4:],
                             elapsed_s=round(elapsed, 1))
                except Exception as e:
                    log.warning("typing_keeper_ack_falhou", erro=str(e)[:80])

    def _send_presence_once(self) -> None:
        try:
            self._client.send_presence(self.instance, self.to_number,
                                       presence="composing", delay_ms=25000)
        except Exception as e:
            log.warning("typing_keeper_presence_falhou", erro=str(e)[:80])
