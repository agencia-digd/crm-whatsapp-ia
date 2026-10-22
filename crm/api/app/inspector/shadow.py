"""
Modo sombra (aprendiz) — a IA decide o que RESPONDERIA a um lead, mas NUNCA envia.

Para uma conversa onde o LEAD falou por último (está esperando resposta), a IA:
- decide um BUCKET de autoconfiança:
    responderia_sozinha  → sabe responder com segurança (confiança alta)
    rascunharia          → tem um rascunho mas pede revisão humana (confiança média)
    chamaria_voce        → não sabe / assunto sensível → humano (confiança baixa)
- estima CONFIANÇA 0-100
- escreve um RASCUNHO de resposta (no tom da corretora), exceto quando chamaria_voce
- diz o MOTIVO em 1 frase

NADA é enviado. Só grava em `shadow_decisions`. Isso alimenta o placar do Aprendiz
e, na fase seguinte, o aprendizado (comparar com a resposta real da corretora).

Saída JSON do LLM:
  {"bucket": "responderia_sozinha|rascunharia|chamaria_voce",
   "confianca": <int 0-100>,
   "resposta_sugerida": <texto curto no tom dela | null>,
   "motivo": <1 frase, máx 160 chars>}

Se LLM/parse falhar → None + log warning. Não quebra nada.
"""
from __future__ import annotations

import json as _json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog

from app.config import get_llm_provider_shadow
from app.llm.cli_provider import e_limite_de_cota
from app.llm.provider import LLMProvider

log = structlog.get_logger()

BUCKETS_VALIDOS = {"responderia_sozinha", "rascunharia", "chamaria_voce"}

# Assuntos que NUNCA viram resposta sozinha (trava de segurança imobiliária):
# preço/proposta/negociação/jurídico → sempre humano.
_SENSIVEIS_HINT = (
    "Trate como chamaria_voce (humano) qualquer assunto sensível: fechar preço, "
    "fazer proposta/contraproposta, condições de pagamento/financiamento, "
    "questões jurídicas/documentação, ou quando o lead está irritado/reclamando."
)


@dataclass
class ShadowResult:
    corretora_id: int
    lead_id: str | None
    lead_nome: str | None
    lead_telefone: str | None
    bucket: str
    confianca: int
    resposta_sugerida: str | None
    motivo: str | None
    ultima_msg_lead: str | None
    ultima_msg_at: datetime | None
    modelo: str | None


_SYSTEM = (
    "Você é a assistente-aprendiz de uma corretora imobiliária. Está em MODO SOMBRA: "
    "observa a conversa real com o lead e decide o que VOCÊ responderia — mas você NUNCA "
    "envia nada, só treina pra ganhar confiança.\n\n"
    "Sua tarefa: olhar a última mensagem do LEAD (que está esperando resposta) e decidir "
    "se você conseguiria responder sozinha, com que confiança.\n\n"
    "BUCKETS:\n"
    "- responderia_sozinha: pergunta simples/operacional que você sabe responder com "
    "segurança (ex: horário de visita, se ainda tem o imóvel, pedir dado pra qualificar, "
    "reengajar quem sumiu). Confiança 75-100.\n"
    "- rascunharia: você tem uma boa resposta mas vale a corretora revisar antes (ex: "
    "explicar um imóvel específico, propor visita com detalhe). Confiança 45-74.\n"
    "- chamaria_voce: não sabe ou é sensível → humano. Confiança 0-44.\n\n"
    f"{_SENSIVEIS_HINT}\n\n"
    "TOM do rascunho: caloroso, direto, português do Brasil informal, como uma corretora "
    "boa fala no WhatsApp — sem parecer robô, sem jargão, frases curtas. Use o histórico "
    "pra imitar o estilo da corretora.\n\n"
    # A REGRA DO PRÓXIMO PASSO (Dona, 05/08). Medido em 182 rascunhos: 63% não
    # pediam nada, e 21% se DESPEDIAM de conversa que ainda estava viva.
    # "Fico à disposição" é o jeito mais educado de fazer um lead sumir — e a
    # conversa que morre aqui não volta.
    "REGRA DO PRÓXIMO PASSO — a mais importante:\n"
    "Toda mensagem termina com UM próximo passo, e só um. Ou você PERGUNTA algo, "
    "ou você se COMPROMETE com prazo ('te mando as opções ainda hoje').\n"
    "- Se a bola fica com o LEAD → termine perguntando. Uma pergunta só, fácil de "
    "responder, ligada ao que ele acabou de falar.\n"
    "- Se a bola fica com VOCÊ (vai buscar imóvel, confirmar horário, falar com o "
    "proprietário) → termine com o compromisso E o prazo. Sem prazo não é compromisso.\n"
    "- NUNCA termine com despedida ('fico à disposição', 'qualquer coisa me chama', "
    "'boa sorte na busca') a não ser que o LEAD tenha se despedido primeiro ou dito "
    "que não quer mais. Despedida encerra conversa viva.\n"
    "- Não empilhe duas perguntas: quem recebe duas responde uma, ou nenhuma.\n\n"
    "Responda APENAS JSON válido."
)

_SCHEMA: dict[str, Any] = {
    "bucket": "responderia_sozinha | rascunharia | chamaria_voce",
    "confianca": "inteiro 0-100, coerente com o bucket",
    "resposta_sugerida": "rascunho curto no tom da corretora (ou null se chamaria_voce)",
    "motivo": "1 frase curta explicando a decisão, máx 160 chars",
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
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _ultima_do_lead(conv: list[dict[str, Any]]) -> tuple[str | None, datetime | None]:
    for m in reversed(conv):
        if m.get("from") == "lead":
            at = m.get("at")
            dt: datetime | None = None
            if at:
                try:
                    dt = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
                except Exception:
                    dt = None
            return (m.get("text") or f"[{m.get('type', 'msg')}]"), dt
    return None, None


def _system_com_correcoes(corretora_id: int | None) -> str:
    """_SYSTEM + correções humanas recentes. Com a flag OFF devolve o próprio _SYSTEM."""
    from app.services.aprendiz_correcoes import bloco_para_prompt

    bloco = bloco_para_prompt(corretora_id)
    return _SYSTEM + bloco if bloco else _SYSTEM


class ShadowLimiteCota(RuntimeError):
    """Cota/limite da assinatura do `claude -p` estourou: a rodada deve parar, sem insistir."""


def decidir_sombra(
    corretora_nome: str,
    corretora_id: int,
    lead_nome: str | None,
    lead_id: str | None,
    lead_telefone: str | None,
    conversa: list[dict[str, Any]],
    provider: LLMProvider | None = None,
    modelo: str | None = None,
) -> ShadowResult | None:
    """Decide (sem enviar) o que a IA responderia. Retorna ShadowResult ou None."""
    if not conversa:
        return None
    # Só faz sentido quando o lead falou por último (está esperando).
    if conversa[-1].get("from") != "lead":
        log.info("shadow_skip_corretora_falou_por_ultimo",
                 corretora_id=corretora_id, lead_id=lead_id)
        return None

    ultima_lead, ultima_at = _ultima_do_lead(conversa)

    prov = provider or get_llm_provider_shadow()
    user_prompt = (
        f"Corretora: {corretora_nome} | Lead: {lead_nome or 'sem nome'}\n\n"
        f"CONVERSA (a última linha é a do LEAD esperando resposta):\n"
        f"{_format_conversation(conversa)}"
    )

    try:
        raw, _usage = prov.complete(_system_com_correcoes(corretora_id), user_prompt, _SCHEMA)
    except Exception as e:  # noqa: BLE001
        if e_limite_de_cota(str(e)):
            raise ShadowLimiteCota(str(e)) from e
        log.warning("shadow_llm_error", error=str(e),
                    corretora_id=corretora_id, lead_id=lead_id)
        return None

    try:
        data = _json.loads(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("shadow_parse_error", error=str(e),
                    corretora_id=corretora_id, lead_id=lead_id, raw_preview=raw[:200])
        return None

    if not isinstance(data, dict):
        return None

    bucket = data.get("bucket")
    if bucket not in BUCKETS_VALIDOS:
        log.warning("shadow_bucket_invalido", bucket=bucket,
                    corretora_id=corretora_id, lead_id=lead_id)
        return None

    try:
        confianca = int(data.get("confianca", 0))
    except (TypeError, ValueError):
        confianca = 0
    confianca = max(0, min(100, confianca))

    # chamaria_voce nunca carrega rascunho (é por definição "não sei / humano").
    resposta = None if bucket == "chamaria_voce" else _truncate(data.get("resposta_sugerida"), 600)

    return ShadowResult(
        corretora_id=corretora_id,
        lead_id=lead_id,
        lead_nome=lead_nome,
        lead_telefone=lead_telefone,
        bucket=bucket,
        confianca=confianca,
        resposta_sugerida=resposta,
        motivo=_truncate(data.get("motivo"), 160),
        ultima_msg_lead=_truncate(ultima_lead, 600),
        ultima_msg_at=ultima_at,
        modelo=modelo,
    )


def gravar_sombra(res: ShadowResult) -> int:
    """INSERT em shadow_decisions. Retorna id. NUNCA envia nada."""
    from app.db.models import ShadowDecision as Model
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        rec = Model(
            corretora_id=res.corretora_id,
            lead_id=res.lead_id,
            lead_nome=res.lead_nome,
            lead_telefone=res.lead_telefone,
            bucket=res.bucket,
            confianca=res.confianca,
            resposta_sugerida=res.resposta_sugerida,
            motivo=res.motivo,
            ultima_msg_lead=res.ultima_msg_lead,
            ultima_msg_at=res.ultima_msg_at,
            modelo=res.modelo,
        )
        s.add(rec)
        s.commit()
        s.refresh(rec)
        return rec.id
