"""
Análise profunda de leads — via LLMProvider (CLI no MVP, API no SaaS).

Roda APENAS nos leads marcados deserves_deep_analysis=True pelo classifier.
Retorna None se a conversa não justificar análise (sem msgs do lead).
"""
import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError

from app.config import get_llm_provider
from app.inspector.render_conversa import (
    _at_brt,  # re-export p/ compat (usado por testes/pipeline legado)
    render_conversa,
    render_message as _format_message,  # alias p/ compat: classifier importa daqui
)
from app.llm.provider import LLMProvider
from app.services.atendimento_brain import brain

log = structlog.get_logger()

# Silêncio do lead acima disso vira frame de REATIVAÇÃO no prompt (I4).
STALENESS_REATIVACAO_DIAS = 14

__all__ = [
    "STALENESS_REATIVACAO_DIAS",
    "DeepAnalysisResult",
    "analyze_lead",
    "dias_sem_msg_lead",
    "verificar_citacao",
    "validar_respiro",
    "_format_message",
    "_at_brt",
]

_SYSTEM = (
    brain(sugerir=True) + "\n\n"
    "TAREFA: analise a conversa real e retorne APENAS JSON válido. "
    "gap_identificado = onde a conversa fugiu do MÉTODO (ex: apresentou imóvel antes de descobrir o desejo/gatilho; "
    "falou mais do que perguntou; não explorou uma deixa emocional; não descobriu quem decide) — construtivo ('poderia ter...'), nunca acusatório. "
    "abordagem_sugerida = a PRÓXIMA mensagem pronta pra corretora mandar, seguindo o método (geralmente uma pergunta diagnóstica calorosa, não um pitch). "
    "\n\nANTES DE APONTAR QUALQUER GAP (obrigatório): "
    "liste MENTALMENTE (a) as perguntas que a corretora JÁ fez na conversa e "
    "(b) as respostas/informações que o lead JÁ deu. "
    "É PROIBIDO acusar 'não fez pergunta diagnóstica' / 'pulou o diagnóstico' se a corretora "
    "de fato fez perguntas — nesse caso o gap é OUTRO ou é null. "
    "É PROIBIDO sugerir na abordagem uma pergunta que o lead JÁ respondeu "
    "(ex: perguntar 'morar ou investir?' quando ele já disse). "
    "O gap_identificado SÓ é válido se sustentado por um comportamento OBSERVADO na conversa — "
    "cite o momento (o que foi dito). Se não houver gap sustentável, gap_identificado = null. "
    "\n\nFormato de mensagens: [HH:MM] ROLE: conteúdo. "
    "Mensagens marcadas como [áudio]: \"texto\" foram FALADAS, não digitadas — "
    "use a transcrição entre aspas como conteúdo real. "
    "Esse formato indica que a corretora ou o lead gravou áudio em vez de digitar: "
    "relevante para avaliar tom, urgência e formato de comunicação preferido. "
    "[áudio em processamento] = transcrição ainda não disponível. "
    "[áudio não disponível] = arquivo de mídia expirado, conteúdo desconhecido. "
    "[imagem]: \"texto\" = imagem com legenda; [imagem sem legenda] = imagem sem texto."
)

_SCHEMA: dict[str, Any] = {
    "conversa_resumo": "2-3 frases sobre o que foi discutido e situação atual do lead",
    "o_que_corretora_disse": "trecho mais relevante que a corretora disse (ou null)",
    "gap_identificado": "o que a corretora poderia ter feito melhor (ou null se não há gap claro)",
    "abordagem_sugerida": "sugestão de próxima mensagem para avançar (ou null se negócio já avançado)",
}


class DeepAnalysisResult(BaseModel):
    conversa_resumo: str
    o_que_corretora_disse: str | None
    gap_identificado: str | None
    abordagem_sugerida: str | None
    tokens_used: int = 0
    # Auditoria dos guardrails (I4/I5/I6) — gravados no raw_analysis
    dias_sem_msg_lead: int | None = None
    citacao_verificada: bool = True
    respiro_valido: bool = True


# ── Guardrails baratos pós-LLM (I4 / I5 / I6) ─────────────────────────────────

def dias_sem_msg_lead(conversation: list[dict[str, Any]], now: datetime | None = None) -> int | None:
    """
    Dias corridos desde a ÚLTIMA mensagem DO LEAD (não da corretora).

    Retorna None se o lead nunca falou. Usado pra frame de reativação (I4).
    """
    now = now or datetime.now(timezone.utc)
    last_lead_at: datetime | None = None
    for m in conversation:
        if m.get("from") != "lead":
            continue
        at_str = m.get("at") or ""
        if not at_str:
            continue
        try:
            dt = datetime.fromisoformat(at_str)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if last_lead_at is None or dt > last_lead_at:
            last_lead_at = dt
    if last_lead_at is None:
        return None
    delta = now - last_lead_at
    return max(delta.days, 0)


def _reativacao_frame(dias: int) -> str:
    """Frame de REATIVAÇÃO injetado no prompt quando o lead está em silêncio (I4)."""
    return (
        "\n\n⚠️ CONTEXTO DE STALENESS (obrigatório): este lead está EM SILÊNCIO há "
        f"{dias} dias — a última mensagem dele é antiga. "
        "A abordagem_sugerida deve ser de REATIVAÇÃO leve e honesta, NÃO de continuidade. "
        "É PROIBIDO fingir que a conversa está fluindo agora (ex: 'que bom te ter aqui!', "
        "'como combinamos', 'seguindo nosso papo'). "
        "Referencie o ÚLTIMO ASSUNTO REAL da conversa e reconheça o tempo passado com naturalidade "
        f"(ex: 'faz um tempinho que a gente não se fala'). "
        "Não trate o estado do lead como atual — ele esfriou."
    )


_STOPWORDS = {
    "a", "o", "e", "de", "da", "do", "das", "dos", "que", "com", "para", "por",
    "uma", "um", "os", "as", "no", "na", "nos", "nas", "se", "meu", "minha",
    "seu", "sua", "pra", "pro", "ele", "ela", "voce", "você", "isso", "esse",
    "essa", "mais", "mas", "como", "sim", "nao", "não", "ja", "já", "tem",
    "ter", "foi", "sao", "são", "está", "esta", "estou", "aqui", "muito",
    "bem", "vou", " vai", "vai", "faz", "ate", "até", "tudo", "sobre", "dele",
    "dela", "quando", "onde", "qual", "seus", "suas",
}


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )


def _significant_words(text: str) -> list[str]:
    """Palavras significativas (>3 chars, sem acento, sem stopword) de um texto."""
    norm = _strip_accents((text or "").lower())
    tokens = re.findall(r"[a-z0-9]+", norm)
    return [t for t in tokens if len(t) > 3 and t not in _STOPWORDS]


def verificar_citacao(trecho: str | None, conversa_render: str, threshold: float = 0.6) -> bool:
    """
    True se ~threshold+ das palavras significativas do trecho citado aparecem na
    conversa renderizada (tolerante a paráfrase leve). I6.1.

    Trecho vazio/None → True (nada a verificar).
    """
    if not trecho or not trecho.strip():
        return True
    palavras = _significant_words(trecho)
    if not palavras:
        return True
    conv_words = set(_significant_words(conversa_render))
    hits = sum(1 for w in palavras if w in conv_words)
    return (hits / len(palavras)) >= threshold


def _conta_perguntas_finais(msg: str) -> int:
    """
    Conta '?' relevantes na abordagem: pega a última frase não-vazia (respiro em
    linhas) e conta quantos '?' ela tem. Usado pra validar 'termina com 1 pergunta'.
    """
    return (msg or "").count("?")


def validar_respiro(abordagem: str | None) -> bool:
    """
    True se a abordagem_sugerida termina com EXATAMENTE 1 pergunta (I5.2).

    Regra: a mensagem tem exatamente 1 '?' no total E o último caractere
    não-espaço é '?'. Isso rejeita: termina em afirmação (0 '?'),
    ou dupla pergunta (2+ '?').
    None/vazio → True (não bloqueia — abordagem pode ser legitimamente null).
    """
    if not abordagem or not abordagem.strip():
        return True
    stripped = abordagem.strip()
    if _conta_perguntas_finais(stripped) != 1:
        return False
    return stripped.endswith("?")


def analyze_lead(
    lead_data: dict[str, Any],
    conversation: list[dict[str, Any]],
    corretora_nome: str = "",
    provider: LLMProvider | None = None,
    dias_silencio_lead: int | None = None,
) -> DeepAnalysisResult | None:
    """
    Analisa profundamente a conversa para coaching da corretora.

    Pré-condição: conversation deve ter >= 1 msg do lead.
    Se não houver msgs do lead: retorna None sem chamar o provider.

    Args:
        lead_data: {lead_id, nome, ...}
        conversation: mensagens normalizadas filtradas pela janela de conexão
        corretora_nome: nome da corretora para contextualizar o prompt
        provider: LLMProvider; se None, usa get_llm_provider() (injeção p/ testes)
        dias_silencio_lead: dias desde a última msg do lead; se None, é calculado
            internamente. > STALENESS_REATIVACAO_DIAS injeta frame de reativação (I4).

    Returns:
        DeepAnalysisResult ou None. Em erro após 2 tentativas: None.
    """
    lead_id = lead_data.get("lead_id")
    lead_msgs = [m for m in conversation if m.get("from") == "lead"]
    if not lead_msgs:
        log.info("analyzer_skip_no_lead_msgs", lead_id=lead_id)
        return None

    if provider is None:
        provider = get_llm_provider()

    dias_silencio = (
        dias_silencio_lead
        if dias_silencio_lead is not None
        else dias_sem_msg_lead(conversation)
    )

    conversa_render = render_conversa(conversation)
    system = _SYSTEM
    if dias_silencio is not None and dias_silencio > STALENESS_REATIVACAO_DIAS:
        system = _SYSTEM + _reativacao_frame(dias_silencio)
        log.info("analyzer_reativacao_frame", lead_id=lead_id, dias=dias_silencio)

    base_user_prompt = (
        f"Corretora: {corretora_nome or 'a corretora'} | Lead: {lead_data.get('nome', '?')}\n\n"
        f"CONVERSA:\n{conversa_render}"
    )

    # ── Loop de guardrails: cada iteração é 1 chamada ao provider. ────────────
    # 2 tentativas base (erro de output) + até 1 retry extra por guardrail
    # (citação/respiro) reforçando a instrução. Máx. 3 chamadas.
    guardrail_retry_used = False
    for attempt in range(3):
        reinforce = ""
        if guardrail_retry_used:
            reinforce = (
                "\n\nREVISÃO OBRIGATÓRIA (sua resposta anterior falhou um check):\n"
                "- o_que_corretora_disse DEVE ser uma citação LITERAL de algo que a "
                "corretora escreveu na conversa acima (copie o trecho, não parafraseie).\n"
                "- abordagem_sugerida DEVE terminar com EXATAMENTE UMA pergunta "
                "(um único '?', no fim da mensagem)."
            )
        user_prompt = base_user_prompt + reinforce
        try:
            raw, usage = provider.complete(system, user_prompt, _SCHEMA)
            data = json.loads(raw)
            result = DeepAnalysisResult(**data, tokens_used=usage.total)
        except (ValidationError, TypeError, KeyError) as exc:
            log.warning(
                "analyzer_invalid_output",
                lead_id=lead_id, error=str(exc), attempt=attempt + 1,
            )
            if attempt >= 1:
                return None
            continue
        except RuntimeError:
            log.exception("analyzer_provider_error", lead_id=lead_id, attempt=attempt + 1)
            if attempt >= 1:
                return None
            continue
        except Exception:
            log.exception("analyzer_error", lead_id=lead_id, attempt=attempt + 1)
            if attempt >= 1:
                return None
            continue

        # ── Guardrails pós-LLM (I5.2 respiro + I6.1 citação) ──────────────────
        result.dias_sem_msg_lead = dias_silencio
        citacao_ok = verificar_citacao(result.o_que_corretora_disse, conversa_render)
        respiro_ok = validar_respiro(result.abordagem_sugerida)

        if (not citacao_ok or not respiro_ok) and not guardrail_retry_used:
            guardrail_retry_used = True
            log.info(
                "analyzer_guardrail_retry",
                lead_id=lead_id, citacao_ok=citacao_ok, respiro_ok=respiro_ok,
            )
            continue  # 1 retry reforçado

        # Aceita o resultado (retry já usado ou tudo ok). Loga falhas residuais.
        if not citacao_ok:
            log.warning("citacao_nao_verificada", lead_id=lead_id,
                        trecho=(result.o_que_corretora_disse or "")[:120])
            result.o_que_corretora_disse = None
            result.citacao_verificada = False
        if not respiro_ok:
            log.warning("respiro_invalido", lead_id=lead_id,
                        abordagem=(result.abordagem_sugerida or "")[:120])
            result.respiro_valido = False

        log.info(
            "analyzer_ok",
            lead_id=lead_id,
            has_gap=bool(result.gap_identificado),
            tokens=usage.total,
            attempt=attempt + 1,
            dias_silencio=dias_silencio,
            citacao_verificada=result.citacao_verificada,
            respiro_valido=result.respiro_valido,
        )
        return result

    return None
