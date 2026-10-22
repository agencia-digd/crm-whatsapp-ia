"""
Responder da Corretor — gera a próxima mensagem da conversa.

Não é um Agno Agent (event-driven, não scheduled). É uma função pura:
  (system_prompt, histórico de mensagens) → (texto pro lead, sinal de estado)

Cérebro: `get_llm_provider()` — o MESMO do Inspector e da Assistente. Migra junto
quando o LLM_PROVIDER virar codex, entra na sombra junto, e herda o fallback.

Histórico (28/07/2026): chamava `claude -p --model claude-opus-4-7` por
subprocess, furando o provider. Consequência: (a) ficaria pra trás na migração,
seguindo pendurada na assinatura pessoal da Dona; (b) Opus — o modelo mais caro —
fixado em código. Estava dormente quando foi corrigido; a dívida foi paga antes
de alguém religar.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import structlog

from app.recovery.prompts import CORRETOR_SYSTEM_PROMPT

if TYPE_CHECKING:
    from app.llm.provider import LLMProvider

log = structlog.get_logger()

TIMEOUT_S = 90

_STATE_RE = re.compile(r"<state>(.*?)</state>", re.DOTALL)

EndReason = Literal["ja_comprou", "sem_interesse", "aluguel", "opt_out", "pediu_humano"]


@dataclass
class BiaReply:
    text: str                       # mensagem limpa pra mandar pro lead
    qualified: bool
    ended: bool
    end_reason: EndReason | None
    raw_output: str                 # output bruto do CLI (debug)


@dataclass
class Message:
    role: Literal["lead", "corretor"]
    text: str


def generate_reply(
    lead_name: str | None,
    history: list[Message],
    *,
    timeout: int = TIMEOUT_S,
    provider: "LLMProvider | None" = None,
) -> BiaReply:
    """
    Gera a próxima mensagem da Corretor dado o histórico da conversa.

    history: lista cronológica de Messages (lead e Corretor intercalados).
             A última deve ser do lead (é a que dispara a resposta).
    provider: injeção pra teste; None = get_llm_provider() (cérebro do sistema).
    """
    if not history or history[-1].role != "lead":
        raise ValueError("generate_reply: histórico vazio ou última msg não é do lead")

    user_block = _format_history(lead_name, history)
    prompt = f"{CORRETOR_SYSTEM_PROMPT}\n\n---\n\n{user_block}\n\nResponda como Corretor."

    output = _run_llm(prompt, provider=provider)
    text, qualified, ended, reason = _parse_output(output)

    return BiaReply(
        text=text,
        qualified=qualified,
        ended=ended,
        end_reason=reason,
        raw_output=output,
    )


def _format_history(lead_name: str | None, history: list[Message]) -> str:
    lines = [f"Lead: {lead_name or '(nome não informado)'}", "", "Conversa até agora:"]
    for msg in history:
        who = "LEAD" if msg.role == "lead" else "CORRETOR"
        lines.append(f"[{who}] {msg.text}")
    return "\n".join(lines)


def _run_llm(prompt: str, *, provider: "LLMProvider | None" = None) -> str:
    """Chama o cérebro configurado. Texto livre: a Corretor devolve a mensagem do lead
    + um bloco <state>, então NÃO usar complete() (que espera JSON puro)."""
    from app.config import get_llm_provider

    prov = provider or get_llm_provider()
    output, usage = prov.complete_text("", prompt)
    log.info(
        "corretor_responder_call",
        provider=type(prov).__name__,
        input_chars=len(prompt),
        output_chars=len(output),
        tokens=usage.total,
    )
    return output


def _parse_output(raw: str) -> tuple[str, bool, bool, EndReason | None]:
    """Separa o texto pro lead do marcador <state>...</state>."""
    match = _STATE_RE.search(raw)
    if not match:
        text = raw.strip()
        return text, False, False, None

    state_json = match.group(1).strip()
    text = _STATE_RE.sub("", raw).strip()

    try:
        state = json.loads(state_json)
    except json.JSONDecodeError:
        log.warning("corretor_state_invalid_json", raw=state_json[:200])
        return text, False, False, None

    qualified = bool(state.get("qualified", False))
    ended = bool(state.get("ended", False))
    reason_raw = state.get("reason")
    reason: EndReason | None = reason_raw if reason_raw in {
        "ja_comprou", "sem_interesse", "aluguel", "opt_out", "pediu_humano"
    } else None

    return text, qualified, ended, reason
