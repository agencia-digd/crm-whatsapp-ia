"""
Reformatação de leads pra briefing matinal.

lead_analyses é gerado pelo Inspector pra Dona (3ª pessoa: "Corretora pediu...").
Pra mandar pra corretora, vira 2ª pessoa: "Você pediu...". LLM via cli_provider.

Output enxuto: 1 frase de contexto + 1-2 frases de sugestão pronta.
Se LLM falhar: fallback usando os campos crus.
"""
from __future__ import annotations

from typing import Any

import json as _json
import structlog

from app.config import get_llm_provider
from app.llm.provider import LLMProvider
from app.services.briefing_corretora import LeadRetomar

log = structlog.get_logger()


SYSTEM_PROMPT = (
    "Você reformata insights sobre leads de imóveis pra mandar via WhatsApp pra "
    "uma corretora. Recebe contexto em 3ª pessoa e devolve em 2ª pessoa, "
    "como se estivesse falando direto com ela. Tom: técnico, gentil, direto. "
    "Nada de julgamento, nada de bajulação. Português do Brasil informal."
)


def _build_user_prompt(corretora_nome: str, leads: list[LeadRetomar]) -> str:
    blocos = []
    for i, l in enumerate(leads, 1):
        blocos.append(
            f"[{i}] Lead: {l.nome}\n"
            f"Contexto/o que foi dito: {l.contexto}\n"
            f"Abordagem sugerida (rascunho): {l.mensagem_sugerida or '(sem rascunho)'}"
        )
    leads_txt = "\n\n".join(blocos)
    return (
        f"Corretora destinatária: {corretora_nome}.\n\n"
        f"Reformate os {len(leads)} leads abaixo em 2ª pessoa pra essa corretora. "
        f"Pra cada lead, gere:\n"
        f"- voce_disse: 1 frase curta (≤25 palavras) descrevendo o que VOCÊ "
        f"(corretora) fez ou o gap. Começa com verbo na 2ª pessoa "
        f"(ex: 'Você pediu...', 'Você não sugeriu...').\n"
        f"- sugestao: mensagem PRONTA pra mandar ao lead, do jeito que a "
        f"corretora falaria (1-2 frases, máximo 40 palavras, sem 'Olá' formal).\n\n"
        f"LEADS:\n{leads_txt}"
    )


JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "leads": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "nome": {"type": "string"},
                    "voce_disse": {"type": "string"},
                    "sugestao": {"type": "string"},
                },
                "required": ["nome", "voce_disse", "sugestao"],
            },
        }
    },
    "required": ["leads"],
}


def _fallback(leads: list[LeadRetomar]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for l in leads:
        out.append({
            "nome": l.nome,
            "voce_disse": l.contexto,
            "sugestao": l.mensagem_sugerida or "",
        })
    return out


def reformatar_leads(
    corretora_nome: str,
    leads_crus: list[LeadRetomar],
    provider: LLMProvider | None = None,
) -> list[dict[str, Any]]:
    """
    Recebe leads brutos do Inspector, devolve list[{nome, voce_disse, sugestao}].

    Em caso de erro LLM ou parse: fallback pros campos crus + log warn.
    """
    if not leads_crus:
        return []

    prov = provider or get_llm_provider()
    user_prompt = _build_user_prompt(corretora_nome, leads_crus)

    try:
        raw, _usage = prov.complete(
            system=SYSTEM_PROMPT,
            user=user_prompt,
            json_schema=JSON_SCHEMA,
        )
    except Exception as e:
        log.warning("lead_reformat_llm_error", error=str(e),
                    corretora=corretora_nome, n_leads=len(leads_crus))
        return _fallback(leads_crus)

    try:
        data = _json.loads(raw)
        items = data.get("leads") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            raise ValueError("missing_leads_array")
    except Exception as e:
        log.warning("lead_reformat_parse_error", error=str(e),
                    corretora=corretora_nome, raw_preview=raw[:200])
        return _fallback(leads_crus)

    by_name = {(it.get("nome") or "").strip(): it for it in items
               if isinstance(it, dict)}
    out: list[dict[str, Any]] = []
    for l in leads_crus:
        it = by_name.get(l.nome) or {}
        voce = (it.get("voce_disse") or "").strip()
        sug = (it.get("sugestao") or "").strip()
        if not voce and not sug:
            out.append({
                "nome": l.nome,
                "voce_disse": l.contexto,
                "sugestao": l.mensagem_sugerida or "",
            })
        else:
            out.append({
                "nome": l.nome,
                "voce_disse": voce or l.contexto,
                "sugestao": sug or (l.mensagem_sugerida or ""),
            })
    return out
