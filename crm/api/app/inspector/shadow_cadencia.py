"""
Sombra da CADÊNCIA — o que a IA escreveria no lugar do template fixo.

Diferença pro `shadow.py` (que é o irmão REATIVO): lá o lead falou por último e
a IA decide o que RESPONDER. Aqui é o lado PROATIVO — o toque N da cadência
venceu, o sistema mandaria `_TEMPLATES_MSG[N]` (texto genérico), e a pergunta é:
tendo lido a conversa inteira, a IA escreveria algo melhor?

Por que isso existe (29/07/2026): a cadência é mala-direta. O Inspector já leu a
conversa, classificou e achou o gap — e mesmo assim o follow-up sai com texto
fixo escolhido por índice. A inteligência está toda na análise, nenhuma na ação.
Este módulo mede se vale mudar isso, sem arriscar nada.

⚠️ ESTE MÓDULO NÃO ENVIA NADA. Por construção: ele não importa nenhum sender,
nenhum cliente de WhatsApp, nenhuma função de disparo. Só lê conversa, chama o
LLM e devolve um dataclass. Quem grava é o runner; quem julga é a Dona.

⚠️ O QUE A IA ESCREVE É SÓ A VARIÁVEL {{2}} do template UTILITY aprovado —
não a mensagem inteira. Fora da janela de 24h a Meta só aceita template
pré-aprovado; a casca ("Olá {{1}}, tudo bem?" ... "Qualquer coisa é só me
chamar por aqui") é fixa. A personalização cabe no miolo. Foi a Dona quem
corrigiu isso (29/07) — a v1 pedia a mensagem toda, que seria inenviável.

Saída JSON do LLM:
  {"corpo": <o miolo da variável {{2}}, sem saudação e sem despedida>,
   "usou_contexto": <bool>,
   "contexto_usado": <o fato concreto da conversa que ela usou | null>,
   "motivo": <1 frase, máx 160 chars>}

Se LLM/parse falhar → None + log warning. Não quebra a cadência (nem é chamado
por ela: o runner roda por fora, em batch).
"""
from __future__ import annotations

import json as _json
from dataclasses import dataclass
from typing import Any

import structlog

from app.config import get_llm_provider
from app.llm.provider import LLMProvider

log = structlog.get_logger()


@dataclass
class ShadowCadenciaResult:
    corretora_id: int
    lead_nome: str | None
    lead_telefone: str | None
    toque: int
    template_atual: str
    sugestao_ia: str | None
    usou_contexto: bool
    contexto_usado: str | None
    motivo: str | None
    modelo: str | None


# Toques que vêm LOGO DEPOIS de uma tarefa de ligação real da corretora.
# A cadência é: D+1 ligação → D+2 mensagem · D+5 ligação → D+7 mensagem.
# Nesses toques o "tentei te ligar" é FATO, não enfeite: a corretora ligou
# mesmo no dia anterior. A Dona (29/07): "precisamos manter pois um dia antes a
# corretora teve a tarefa de ligar". Se a IA apagar isso, a mensagem fica
# incoerente com o que de fato aconteceu.
TOQUES_POS_LIGACAO = {2, 3, 5}

_TRAVA_LIGACAO = (
    "⚠️ CONTEXTO OBRIGATÓRIO DESTE TOQUE: a corretora TENTOU LIGAR pra este lead "
    "nos dias anteriores, de verdade. Sua mensagem PRECISA manter esse "
    "enquadramento — que ela tentou falar por telefone e não conseguiu. Isso não "
    "é enfeite do template, é fato. Você pode dizer com outras palavras, mas não "
    "pode sumir com ele nem inventar que foi o lead quem não atendeu de "
    "propósito.\n"
)

# As mesmas travas do irmão reativo: follow-up NUNCA negocia.
_TRAVAS = (
    "TRAVAS INEGOCIÁVEIS:\n"
    "- NUNCA fale de preço, proposta, contraproposta, condição de pagamento ou "
    "financiamento. Isso é da corretora.\n"
    "- NUNCA prometa nada que você não pode garantir (que o imóvel ainda está "
    "disponível, que consegue desconto, prazo de resposta).\n"
    "- NUNCA invente fato que não está na conversa. Se não tem contexto útil, "
    "diga isso e mantenha a mensagem genérica.\n"
    "- Se o lead demonstrou irritação, pediu pra parar, ou o assunto é "
    "jurídico/documentação: `usou_contexto: false` e mensagem neutra e curta.\n"
)

_SYSTEM = (
    "Você é a assistente de uma corretora imobiliária, em MODO SOMBRA: você "
    "escreve o que MANDARIA, mas NADA é enviado — é um experimento pra comparar "
    "com o texto padrão.\n\n"
    "⚠️ RESTRIÇÃO DA PLATAFORMA (a mais importante): fora da janela de 24h o "
    "WhatsApp só aceita TEMPLATE UTILITY já aprovado pela Meta. A casca é fixa e "
    "NÃO pode mudar:\n\n"
    "    Olá {NOME}, tudo bem?\n"
    "    <<< AQUI ENTRA O QUE VOCÊ ESCREVE — a variável {{2}} >>>\n"
    "    Qualquer coisa é só me chamar por aqui.\n\n"
    "Você escreve APENAS o miolo. Isso significa:\n"
    "- NÃO escreva saudação ('oi', 'olá', 'tudo bem') — a casca já cumprimenta.\n"
    "- NÃO escreva despedida nem assinatura — a casca já fecha.\n"
    "- NÃO use o nome do lead na abertura (a casca já usa). Só no meio, se fizer "
    "sentido.\n\n"
    "Contexto: chegou a hora de um toque de follow-up com um lead que não "
    "responde há dias. Hoje o sistema põe nessa variável um TEXTO PADRÃO igual "
    "pra todo mundo. Você viu a conversa real.\n\n"
    "A pergunta que importa: existe na conversa um fato concreto (um bairro que "
    "ele citou, um imóvel específico, um motivo que ele deu pra sumir, uma data "
    "que ele marcou, uma restrição) que faça esse miolo valer mais que o "
    "padrão?\n"
    "- Se SIM → escreva usando esse fato e informe qual foi em `contexto_usado`.\n"
    "- Se NÃO → `usou_contexto: false` e escreva algo simples. É resposta honesta "
    "e útil: significa que o template já serve pra esse caso.\n\n"
    f"{_TRAVAS}\n"
    "TOM: português do Brasil informal, como corretora boa fala no WhatsApp. "
    "Frases curtas, sem jargão, sem parecer robô.\n\n"
    "TAMANHO — leve a sério: no MÁXIMO 2 frases, terminando em pergunta. "
    "Follow-up curto é lido; follow-up longo é ignorado (e a variável de "
    "template não quebra linha bem). Prefira 1 frase + a pergunta. Não use "
    "quebra de linha.\n\n"
    "Responda APENAS JSON válido."
)

_SCHEMA: dict[str, Any] = {
    "corpo": (
        "SÓ o miolo da variável {{2}} — sem saudação, sem despedida. "
        "Máx 300 chars, terminando em pergunta."
    ),
    "usou_contexto": "true se usou um fato concreto da conversa, false se genérica",
    "contexto_usado": "o fato da conversa que você usou (curto) ou null",
    "motivo": "1 frase explicando a escolha, máx 160 chars",
}

# Saudação/despedida no miolo = template duplicado ("Olá X, tudo bem? Oi X!").
# Detecta e sinaliza em vez de deixar passar.
_ABERTURAS_PROIBIDAS = ("oi ", "oi,", "olá", "ola ", "bom dia", "boa tarde", "boa noite", "tudo bem")


def _parece_ter_saudacao(corpo: str) -> bool:
    ini = corpo.strip().lower()[:24]
    return any(ini.startswith(p) for p in _ABERTURAS_PROIBIDAS)


# ── Quebra de linha dentro de variável de template ───────────────────────────
# A Meta REJEITA \n, \t e 4+ espaços dentro do valor de uma variável. O único
# caractere que passa e que o WhatsApp renderiza como quebra é o U+000B
# (VERTICAL TAB, `\v`, chr(11)). Confirmado 21/06/2026 — ver
# `projetos/api/TEMPLATES-prospeccao-agencia.md` e a skill `templates-utility-api`.
#
# Sanitizamos na SAÍDA em vez de confiar que o modelo obedeça: ele escreve
# natural, a gente normaliza pro que a Meta aceita.
VT = "\v"  # U+000B


def sanitizar_variavel(corpo: str) -> str:
    """Deixa o texto no formato que a Meta aceita dentro de uma variável.

    - `\\n` (e `\\r\\n`) viram U+000B
    - `\\t` vira espaço
    - 4+ espaços seguidos viram 1 (a Meta rejeita)
    - VTs repetidos colapsam (parágrafo duplo não ajuda em texto curto)
    """
    s = corpo.replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("\n", VT).replace("\t", " ")
    while "   " in s:            # 3+ espaços já é risco; corta em 1
        s = s.replace("   ", " ")
    s = s.replace("  ", " ")
    while VT + VT in s:
        s = s.replace(VT + VT, VT)
    return s.strip().strip(VT).strip()


def _format_conversation(conv: list[dict[str, Any]]) -> str:
    if not conv:
        return "[sem mensagens]"
    linhas = []
    for m in conv:
        quem = "CORRETORA" if m.get("from") == "corretora" else "LEAD"
        txt = (m.get("text") or f"[{m.get('type', 'msg')}]")[:500]
        at = (m.get("at") or "")[:16].replace("T", " ")
        linhas.append(f"[{at}] {quem}: {txt}")
    return "\n".join(linhas)


def _truncate(s: str | None, limit: int) -> str | None:
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def decidir_sombra_cadencia(
    corretora_nome: str,
    corretora_id: int,
    lead_nome: str | None,
    lead_telefone: str | None,
    toque: int,
    template_atual: str,
    conversa: list[dict[str, Any]],
    provider: LLMProvider | None = None,
    modelo: str | None = None,
) -> ShadowCadenciaResult | None:
    """O que a IA mandaria nesse toque, tendo lido a conversa. NÃO ENVIA."""
    if not conversa:
        return None

    prov = provider or get_llm_provider()
    # Toque logo após tarefa de ligação → o "tentei te ligar" é fato e fica.
    trava_extra = _TRAVA_LIGACAO if toque in TOQUES_POS_LIGACAO else ""
    system = _SYSTEM + ("\n" + trava_extra if trava_extra else "")
    user_prompt = (
        f"Corretora: {corretora_nome} | Lead: {lead_nome or 'sem nome'}\n"
        f"Toque da cadência: {toque}\n\n"
        f"TEXTO PADRÃO que o sistema mandaria agora:\n{template_atual}\n\n"
        f"CONVERSA REAL até aqui:\n{_format_conversation(conversa)}"
    )

    try:
        raw, _usage = prov.complete(system, user_prompt, _SCHEMA)
    except Exception as e:  # noqa: BLE001
        log.warning("shadow_cadencia_llm_error", error=str(e),
                    corretora_id=corretora_id, toque=toque)
        return None

    try:
        data = _json.loads(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("shadow_cadencia_parse_error", error=str(e),
                    corretora_id=corretora_id, toque=toque, raw_preview=raw[:200])
        return None

    if not isinstance(data, dict):
        return None

    # aceita "corpo" (contrato atual) e "mensagem" (tolerância a modelo teimoso)
    corpo = _truncate(data.get("corpo") or data.get("mensagem"), 600)
    # normaliza pro que a Meta aceita dentro de variável (\n → U+000B etc.)
    corpo = sanitizar_variavel(corpo) if corpo else corpo
    if not corpo:
        log.warning("shadow_cadencia_sem_corpo", corretora_id=corretora_id, toque=toque)
        return None

    # Se veio com saudação, o miolo duplicaria a casca do template. Só loga —
    # o par vai pro julgamento assim mesmo, e isso vira sinal de ajuste de prompt.
    if _parece_ter_saudacao(corpo):
        log.info("shadow_cadencia_corpo_com_saudacao",
                 corretora_id=corretora_id, toque=toque, preview=corpo[:60])

    usou = bool(data.get("usou_contexto"))
    contexto = _truncate(data.get("contexto_usado"), 300) if usou else None
    # coerência: disse que usou contexto mas não disse qual → trata como genérica
    if usou and not contexto:
        usou = False

    return ShadowCadenciaResult(
        corretora_id=corretora_id,
        lead_nome=lead_nome,
        lead_telefone=lead_telefone,
        toque=toque,
        template_atual=template_atual,
        sugestao_ia=corpo,
        usou_contexto=usou,
        contexto_usado=contexto,
        motivo=_truncate(data.get("motivo"), 160),
        modelo=modelo,
    )
