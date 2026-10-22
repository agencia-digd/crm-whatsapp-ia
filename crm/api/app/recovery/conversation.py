"""
State machine da conversa Corretor ↔ lead.

Pura — sem IO, sem LLM. Recebe estado atual + evento, retorna novo estado.
Persistência fica em `recovery_conversations` (model em db/models.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Literal

REENGAGE_AFTER = timedelta(hours=12)  # silêncio do lead → 1 reengajamento
DROP_AFTER_REENGAGE = timedelta(hours=24)  # se ignorou reengajamento → larga


class State(str, Enum):
    AGUARDANDO = "aguardando"          # HSM enviado, sem resposta ainda
    QUALIFICANDO = "qualificando"      # lead respondeu, Corretor conversando
    REENGAJADO = "reengajado"          # lead travou, Corretor mandou 1 follow-up
    QUALIFICADO = "qualificado"        # 4 dados coletados → move pra Respondida
    PERDIDO = "perdido"                # encerrou (ja_comprou/sem_interesse/aluguel/opt_out)
    LARGADO = "largado"                # silêncio prolongado sem qualificar


EndReason = Literal["ja_comprou", "sem_interesse", "aluguel", "opt_out", "pediu_humano"]


@dataclass
class Conversation:
    lead_id: str
    state: State = State.AGUARDANDO
    turns: int = 0  # quantas msgs a Corretor mandou
    last_lead_msg_at: datetime | None = None
    last_corretor_msg_at: datetime | None = None
    reengage_done: bool = False
    end_reason: EndReason | None = None
    qualified_data: dict = field(default_factory=dict)

    # ── transições ──────────────────────────────────────────────────────────

    def on_lead_message(self, now: datetime) -> None:
        """Lead acabou de mandar mensagem."""
        self.last_lead_msg_at = now
        if self.state in (State.AGUARDANDO, State.REENGAJADO):
            self.state = State.QUALIFICANDO

    def on_corretor_sent(self, now: datetime, qualified: bool, ended: bool,
                    reason: EndReason | None) -> None:
        """Corretor mandou resposta — atualiza estado conforme sinalização do prompt."""
        self.last_corretor_msg_at = now
        self.turns += 1

        if ended and reason:
            self.state = State.PERDIDO
            self.end_reason = reason
            return

        if qualified:
            self.state = State.QUALIFICADO
            return

        # Caso normal: continua qualificando
        self.state = State.QUALIFICANDO

    def on_tick(self, now: datetime) -> Literal["reengage", "drop", "noop"]:
        """
        Chamado periodicamente (cron). Retorna ação a executar.

        - reengage: lead travou há >12h e ainda não foi reengajado
        - drop:     lead ignorou reengajamento por >24h
        - noop:     nada a fazer
        """
        if self.state not in (State.QUALIFICANDO, State.REENGAJADO):
            return "noop"

        if self.last_lead_msg_at is None:
            return "noop"

        silence = now - self.last_lead_msg_at

        if self.state == State.QUALIFICANDO and not self.reengage_done:
            if silence >= REENGAGE_AFTER:
                return "reengage"

        if self.state == State.REENGAJADO:
            if silence >= DROP_AFTER_REENGAGE:
                return "drop"

        return "noop"

    def mark_reengaged(self, now: datetime) -> None:
        self.reengage_done = True
        self.state = State.REENGAJADO
        self.last_corretor_msg_at = now

    def mark_dropped(self) -> None:
        self.state = State.LARGADO


def is_terminal(state: State) -> bool:
    return state in (State.QUALIFICADO, State.PERDIDO, State.LARGADO)
