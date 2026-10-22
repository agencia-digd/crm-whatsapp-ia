"""
Classificação rápida de leads — via LLMProvider (CLI no MVP, API no SaaS).

v2 (2026-05-04):
  - Status DETERMINÍSTICO: última msg do lead → sem_resposta;
    da corretora → atendido. LLM não mais decide status.
  - Temperatura RECALIBRADA: "vou pensar" + engajamento anterior = morno.
    Frio só quando não houve engajamento real.
  - deserves_deep_analysis GENEROSO: morno ou quente → True por override.

Roda em TODOS os leads. Custo: zero API no MVP (Claude CLI / Max plan).
Em caso de falha: fallback seguro (frio, false), status preservado do determinístico.
"""
import json
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError

from app.config import get_classifier_provider
from app.inspector.render_conversa import render_conversa
from app.llm.provider import LLMProvider

log = structlog.get_logger()

_SYSTEM = (
    "Você é um classificador de leads imobiliários. "
    "O STATUS da conversa já foi determinado pela estrutura (quem mandou a última mensagem). "
    "Sua tarefa é decidir APENAS: temperatura do lead e se merece análise profunda. "
    "Retorne APENAS JSON válido. "
    "Formato de mensagens: [YYYY-MM-DD HH:MM] ROLE: conteúdo (horários em Brasília / BRT). "
    "Mensagens com [áudio]: \"texto\" foram FALADAS — use o texto entre aspas como conteúdo real. "
    "[áudio em processamento] / [áudio não disponível] = transcrição indisponível."
)

# Status removido do schema LLM — agora é determinístico
_LLM_SCHEMA: dict[str, Any] = {
    "temperatura": "frio | morno | quente",
    "deserves_deep_analysis": "true | false (boolean)",
    "reason": "1 frase curta explicando a temperatura e intenção do lead",
}

_DEFINITIONS = {
    "temperatura": (
        "QUENTE: lead demonstrou INTENÇÃO CLARA de avançar. "
        "Sinais: pediu visita, falou de financiamento, perguntou data específica, "
        "mencionou pagamento, disse 'quero', 'vou fechar', 'me manda o contrato'. "
        "MORNO: lead ENGAJOU mas SEM decisão clara. "
        "Sinais: fez perguntas (preço, área, condomínio), pediu fotos/detalhes, "
        "disse 'vou pensar', 'te aviso', 'vou ver com minha esposa', 'depois retorno', 'interessante'. "
        "REGRA CRÍTICA: 'vou pensar' + ENGAJAMENTO ANTERIOR (perguntas, interesse) = MORNO, não frio. "
        "FRIO: lead NÃO engajou ou recusou. "
        "Sinais: respondeu monossilábico ('ok', 'valeu'), disse 'não é o que procuro', "
        "'muito caro', ou desapareceu SEM mostrar interesse algum."
    ),
    "deserves_deep_analysis": (
        "True quando: houve TROCA real (lead disse algo + corretora respondeu pelo menos 1x) "
        "E há POTENCIAL DE COACHING (algo que a corretora podia ter feito diferente). "
        "False APENAS quando: conversa muito curta (1-2 msgs sem substância), "
        "lead claramente recusou sem nada a aprender, ou não houve troca alguma."
    ),
}


class _LLMResult(BaseModel):
    """Resultado parcial do LLM — status excluído (determinístico)."""
    temperatura: str
    deserves_deep_analysis: bool
    reason: str


class ClassificationResult(BaseModel):
    status: str
    temperatura: str
    deserves_deep_analysis: bool
    reason: str
    tokens_used: int = 0


_FALLBACK_LLM = _LLMResult(
    temperatura="frio",
    deserves_deep_analysis=False,
    reason="Erro na classificação — fallback seguro aplicado",
)


def _determine_status(conversation: list[dict[str, Any]]) -> str:
    """Status é fato objetivo — determinado pela última mensagem da conversa."""
    if not conversation:
        return "nao_atendido"
    last_sender = conversation[-1].get("from", "")
    if last_sender == "lead":
        return "sem_resposta"
    return "atendido"


def classify_lead(
    lead_data: dict[str, Any],
    conversation: list[dict[str, Any]],
    provider: LLMProvider | None = None,
) -> ClassificationResult:
    """
    Classifica um lead e sua conversa de WhatsApp.

    Status é determinístico (última mensagem de quem).
    LLM decide apenas temperatura + deserves_deep_analysis.
    Override pós-LLM: morno/quente → deserves_deep_analysis = True.

    Args:
        lead_data: {lead_id, nome, etapa_nome, ...}
        conversation: mensagens normalizadas já filtradas pela janela de conexão
        provider: LLMProvider; se None, usa get_llm_provider() (injeção p/ testes)

    Returns:
        ClassificationResult. Em caso de erro irrecuperável: fallback com status preservado.
    """
    # ── Determinístico: status pela última mensagem ──────────────────────────
    status = _determine_status(conversation)

    if status == "nao_atendido":
        return ClassificationResult(
            status="nao_atendido",
            temperatura="frio",
            deserves_deep_analysis=False,
            reason="Sem mensagens na conversa",
            tokens_used=0,
        )

    # ── LLM: temperatura + deserves_deep_analysis + reason ──────────────────
    if provider is None:
        provider = get_classifier_provider()

    n_lead = sum(1 for m in conversation if m.get("from") == "lead")
    n_corretora = sum(1 for m in conversation if m.get("from") == "corretora")

    user_prompt = (
        f"Lead: {lead_data.get('nome', '?')} | Etapa: {lead_data.get('etapa_nome', '?')}\n"
        f"Status (determinístico): {status} "
        f"(última msg: {'do lead — corretora não respondeu' if status == 'sem_resposta' else 'da corretora'})\n\n"
        f"Conversa ({len(conversation)} msgs — {n_lead} do lead, {n_corretora} da corretora):\n"
        f"{render_conversa(conversation)}\n\n"
        f"Definições de temperatura:\n{_DEFINITIONS['temperatura']}\n\n"
        f"Definição de deserves_deep_analysis:\n{_DEFINITIONS['deserves_deep_analysis']}"
    )

    llm_result = _FALLBACK_LLM
    tokens = 0

    for attempt in range(2):
        try:
            raw, usage = provider.complete(_SYSTEM, user_prompt, _LLM_SCHEMA)
            data = json.loads(raw)
            llm_result = _LLMResult(**data)
            tokens = usage.total
            log.info(
                "classifier_ok",
                lead_id=lead_data.get("lead_id"),
                status=status,
                temperatura=llm_result.temperatura,
                tokens=tokens,
                attempt=attempt + 1,
            )
            break
        except (ValidationError, TypeError, KeyError) as exc:
            log.warning(
                "classifier_invalid_output",
                lead_id=lead_data.get("lead_id"),
                error=str(exc),
                attempt=attempt + 1,
            )
        except RuntimeError:
            log.exception("classifier_provider_error", lead_id=lead_data.get("lead_id"), attempt=attempt + 1)
            break
        except Exception:
            log.exception("classifier_error", lead_id=lead_data.get("lead_id"), attempt=attempt + 1)
            break

    # ── Override: morno/quente → deserves_deep_analysis = True ──────────────
    deep = llm_result.deserves_deep_analysis
    if llm_result.temperatura in ("morno", "quente"):
        deep = True

    return ClassificationResult(
        status=status,
        temperatura=llm_result.temperatura,
        deserves_deep_analysis=deep,
        reason=llm_result.reason,
        tokens_used=tokens,
    )
