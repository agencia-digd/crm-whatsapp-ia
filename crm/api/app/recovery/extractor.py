"""
Extractor — lê histórico da conversa Corretor↔lead e produz JSON estruturado +
nota formatada pro Kommo.

Usa Sonnet (mais barato que Opus pra extração estruturada). Via LLMProvider
abstraction, mesmo padrão do Inspector.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Literal

import structlog
from pydantic import BaseModel, Field

from app.llm.provider import LLMProvider
from app.recovery.prompts import EXTRACTOR_SYSTEM_PROMPT
from app.recovery.responder import Message

log = structlog.get_logger()


class QualificationResult(BaseModel):
    """Output validado do extractor."""

    interesse_compra: Literal["sim", "nao", "ja_comprou"] | None = None
    regiao_interesse: str | None = None
    tem_fgts: bool | None = None
    valor_investimento: str | None = None
    motivo_perda: Literal[
        "aluguel", "ja_comprou", "sem_interesse", "opt_out", "pediu_humano"
    ] | None = None
    resumo_nota: str = Field(..., min_length=10)


_SCHEMA = {
    "type": "object",
    "properties": {
        "interesse_compra": {"type": ["string", "null"],
                             "enum": ["sim", "nao", "ja_comprou", None]},
        "regiao_interesse": {"type": ["string", "null"]},
        "tem_fgts": {"type": ["boolean", "null"]},
        "valor_investimento": {"type": ["string", "null"]},
        "motivo_perda": {"type": ["string", "null"],
                         "enum": ["aluguel", "ja_comprou", "sem_interesse",
                                  "opt_out", "pediu_humano", None]},
        "resumo_nota": {"type": "string"},
    },
    "required": ["resumo_nota"],
}


def extract(
    provider: LLMProvider,
    lead_name: str | None,
    history: list[Message],
    *,
    today: date | None = None,
) -> QualificationResult:
    """Roda extração via LLM provider e valida com Pydantic."""
    user_block = _format_history(lead_name, history, today or date.today())
    json_str, usage = provider.complete(
        system=EXTRACTOR_SYSTEM_PROMPT,
        user=user_block,
        json_schema=_SCHEMA,
    )

    log.info(
        "recovery_extractor_call",
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
    )

    data = json.loads(json_str)
    return QualificationResult.model_validate(data)


def _format_history(
    lead_name: str | None, history: list[Message], today: date
) -> str:
    lines = [
        f"Data de hoje: {today.isoformat()}",
        f"Lead: {lead_name or '(nome não informado)'}",
        "",
        "Conversa completa:",
    ]
    for msg in history:
        who = "LEAD" if msg.role == "lead" else "CORRETOR"
        lines.append(f"[{who}] {msg.text}")
    return "\n".join(lines)
