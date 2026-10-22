"""
Coaching observation — Inspector gera 1 observação curta por lead selecionado.

Tom OBRIGATÓRIO:
- Leve, observação curiosa, não julgadora
- Usa "talvez", "vale", "da próxima"
- Nunca "você errou", "esqueceu", "deveria"
- Sem motivacional ("você consegue!")

Saída JSON:
  {"categoria": <uma das 7> | null,
   "trecho_corretora": <citação curta, máx 80 chars>,
   "observacao": <1 frase, máx 140 chars>,
   "sugestao": <mensagem alternativa pronta, máx 200 chars>}

Se categoria=null → sem observação relevante. Retorna None.
Se LLM falhar / parse falhar → None + log warning. Não quebra Inspector.
"""
from __future__ import annotations

import json as _json
from dataclasses import dataclass
from typing import Any, Literal

import structlog

from app.config import get_llm_provider
from app.llm.provider import LLMProvider

log = structlog.get_logger()


from app.persona.playbook_corretora_b import (
    CATEGORIAS_MOVIMENTO,
    aplicar_playbook,
)

# Categorias genéricas de qualidade de conversa (método Pessoa).
CATEGORIAS_GENERICAS = {
    "falta_qualificacao",
    "foco_valor_cedo",
    "resposta_seca",
    "perdeu_deixa",
    "demora_resposta",
    "manual_followup_apos_robo",
    "outro",
}

# Categorias do playbook de fechamento da Pessoa1 (1 por movimento não feito).
# Fonte única: app/persona/playbook_corretora_b.py (MOVIMENTOS).
#   movimento_sem_convite_visita     — não propôs a visita (ou de forma vaga)
#   movimento_visita_sem_horario     — convidou sem 2 horários concretos
#   movimento_sem_tira_friccao       — lead hesitou por tempo e não aliviou
#   movimento_sem_decisor            — não trouxe o decisor (cônjuge/sócio) junto
#   movimento_sem_qualificar_dinheiro— objeção financeira sem qualificar poder de compra
#   movimento_sem_escassez           — havia escassez real e não usou
#   movimento_followup_robotico      — follow-up robótico tendo pista pessoal
CATEGORIAS_VALIDAS = CATEGORIAS_GENERICAS | set(CATEGORIAS_MOVIMENTO)

ORIGENS_VALIDAS = {"lead_quente", "lead_esfriou"}


Origem = Literal["lead_quente", "lead_esfriou"]


@dataclass
class CoachingObservation:
    categoria: str
    trecho_corretora: str | None
    observacao: str
    sugestao: str | None
    origem: Origem
    lead_id: int | None
    lead_nome: str | None
    corretora_id: int


from app.services.atendimento_brain import brain as _brain

_SYSTEM = (
    _brain(sugerir=False) + "\n\n"
    + aplicar_playbook() + "\n\n"
    "Você é a Assistente, uma assistente que faz coaching gentil de corretoras "
    "imobiliárias. Olha conversas reais e identifica UMA observação curta "
    "sobre como a corretora conduziu a conversa (à luz do MÉTODO e do "
    "PLAYBOOK DE FECHAMENTO acima).\n\n"
    "COMO ESCOLHER A OBSERVAÇÃO:\n"
    "- O gargalo do time é atendido→visita. Priorize os movimentos do PLAYBOOK "
    "(categorias movimento_*): se o lead demonstrou interesse e faltou algum "
    "movimento de fechamento, aponte o MAIS relevante — em geral o de propor a "
    "visita amarrando agenda (movimento_sem_convite_visita).\n"
    "- Só use uma categoria genérica (método Pessoa) quando NENHUM movimento de "
    "fechamento se aplica bem.\n"
    "- Ao usar uma categoria movimento_*, a 'sugestao' deve ser uma mensagem "
    "PRONTA no tom da Pessoa11 (curta, calorosa, termina em pergunta), adaptada ao "
    "que esse lead falou — não repita o modelo genérico ao pé da letra.\n\n"
    "TOM OBRIGATÓRIO:\n"
    "- Leve, curiosa, observação respeitosa — nunca julgamento.\n"
    "- Use construções como 'talvez', 'vale', 'da próxima', "
    "'pode ser interessante', 'reparei que'.\n"
    "- NUNCA use: 'você errou', 'você esqueceu', 'deveria', 'tem que', "
    "'precisa fazer'.\n"
    "- NADA de motivacional ('você consegue!', 'vamos lá!', 'arrasa!').\n"
    "- Português do Brasil, informal mas técnico.\n\n"
    "Se a conversa estava boa e não há observação relevante, "
    'retorne {"categoria": null}.\n\n'
    "Responda APENAS JSON válido."
)


_SCHEMA: dict[str, Any] = {
    "categoria": (
        "uma de — MOVIMENTOS DE FECHAMENTO (prefira estes; gargalo atendido→visita): "
        "movimento_sem_convite_visita (lead quente e não propôs visita, ou de forma "
        "vaga sem amarrar agenda) | movimento_visita_sem_horario (convidou mas não "
        "deu 2 horários concretos) | movimento_sem_tira_friccao (lead hesitou por "
        "tempo e não aliviou) | movimento_sem_decisor (apareceu outro decisor e não o "
        "trouxe junto) | movimento_sem_qualificar_dinheiro (objeção financeira sem "
        "qualificar poder de compra como ganho) | movimento_sem_escassez (havia "
        "escassez real e não usou) | movimento_followup_robotico (follow-up robótico "
        "tendo pista pessoal do lead). "
        "GENÉRICAS (método Pessoa, só se nenhum movimento se aplica): "
        "falta_qualificacao | foco_valor_cedo | resposta_seca | perdeu_deixa | "
        "demora_resposta | manual_followup_apos_robo | outro | null. "
        "Use manual_followup_apos_robo SÓ quando o robô (saudação/follow-up "
        "automático) falhou e a corretora não retomou manualmente — não use pra "
        "automações em si."
    ),
    "trecho_corretora": "citação curta do que ela escreveu, máx 80 chars (ou null)",
    "observacao": "1 frase, máx 140 chars",
    "sugestao": "mensagem alternativa pronta, máx 200 chars (ou null)",
}


def _format_conversation(conv: list[dict[str, Any]]) -> str:
    if not conv:
        return "[sem mensagens]"
    lines = []
    for m in conv:
        role = "CORRETORA" if m.get("from") == "corretora" else "LEAD"
        text = (m.get("text") or f"[{m.get('type', 'msg')}]")[:500]
        at = (m.get("at") or "")[:16].replace("T", " ")
        lines.append(f"[{at}] {role}: {text}")
    return "\n".join(lines)


def _truncate(s: str | None, limit: int) -> str | None:
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


def analisar_coaching(
    corretora_nome: str,
    corretora_id: int,
    lead_nome: str,
    lead_id: int | None,
    conversa: list[dict[str, Any]],
    origem: Origem,
    provider: LLMProvider | None = None,
) -> CoachingObservation | None:
    """Devolve UMA observação de coaching, ou None se não houver."""
    if origem not in ORIGENS_VALIDAS:
        raise ValueError(f"origem inválida: {origem!r}")

    # Filtra mensagens de automação enviadas pela corretora.
    # Se sobrar 0 mensagens da corretora: nada a coachar — pula.
    from app.services.deteccao_automacao import is_automacao
    conversa_filtrada = [
        m for m in conversa
        if m.get("from") != "corretora"
        or not is_automacao(corretora_id, m.get("text") or "")
    ]
    msgs_corretora = [m for m in conversa_filtrada if m.get("from") == "corretora"]
    if not msgs_corretora:
        log.info("coaching_skip_so_automacao",
                 corretora_id=corretora_id, lead_id=lead_id,
                 msgs_total=len(conversa))
        return None

    conversa = conversa_filtrada

    prov = provider or get_llm_provider()

    contexto_origem = (
        "Esse lead está QUENTE — observação visa ajustar a conversa pra fechar."
        if origem == "lead_quente"
        else "Esse lead ESFRIOU recentemente — aprendizado retroativo pra próxima vez."
    )
    user_prompt = (
        f"Corretora: {corretora_nome} | Lead: {lead_nome} | Contexto: {contexto_origem}\n\n"
        f"CONVERSA:\n{_format_conversation(conversa)}"
    )

    try:
        raw, _usage = prov.complete(_SYSTEM, user_prompt, _SCHEMA)
    except Exception as e:
        log.warning("coaching_llm_error", error=str(e),
                    corretora_id=corretora_id, lead_id=lead_id)
        return None

    try:
        data = _json.loads(raw)
    except Exception as e:
        log.warning("coaching_parse_error", error=str(e),
                    corretora_id=corretora_id, lead_id=lead_id,
                    raw_preview=raw[:200])
        return None

    if not isinstance(data, dict):
        log.warning("coaching_not_dict", corretora_id=corretora_id, lead_id=lead_id)
        return None

    categoria = data.get("categoria")
    if categoria is None:
        log.info("coaching_no_observation", corretora_id=corretora_id, lead_id=lead_id)
        return None
    if categoria not in CATEGORIAS_VALIDAS:
        log.warning("coaching_categoria_invalida", categoria=categoria,
                    corretora_id=corretora_id, lead_id=lead_id)
        return None

    observacao = _truncate(data.get("observacao"), 140)
    if not observacao:
        log.warning("coaching_observacao_vazia", corretora_id=corretora_id, lead_id=lead_id)
        return None

    return CoachingObservation(
        categoria=categoria,
        trecho_corretora=_truncate(data.get("trecho_corretora"), 80),
        observacao=observacao,
        sugestao=_truncate(data.get("sugestao"), 200),
        origem=origem,
        lead_id=lead_id,
        lead_nome=lead_nome,
        corretora_id=corretora_id,
    )


def gravar_observacao(obs: CoachingObservation) -> int:
    """INSERT em coaching_observations. Retorna id."""
    from app.db.models import CoachingObservation as Model
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        rec = Model(
            corretora_id=obs.corretora_id,
            lead_id=obs.lead_id,
            lead_nome=obs.lead_nome,
            trecho_corretora=obs.trecho_corretora,
            observacao=obs.observacao,
            sugestao=obs.sugestao,
            categoria=obs.categoria,
            origem=obs.origem,
        )
        s.add(rec)
        s.commit()
        s.refresh(rec)
        return rec.id
