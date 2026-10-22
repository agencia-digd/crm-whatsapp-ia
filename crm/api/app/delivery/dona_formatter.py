"""
Formatação do briefing gerencial da Dona (Telegram) — funções puras.

Extraído de `app/agents/agente_assistente.py` (Onda 2 · squad Acme). O agente
segue sendo o entry-point (cron/systemd apontam pra ele); aqui vive só a
MONTAGEM DO TEXTO, testável isolada.

Contrato:
  - Funções deterministas recebem o `context` (dict de build_dona_context) e
    devolvem string(s). Sem acesso a DB, sem envio.
  - Funções que dependem de LLM recebem o `provider` EXPLÍCITO (injeção de
    dependência) — nunca resolvem `get_llm_provider()` por conta própria. Isso
    preserva o ponto de patch dos testes (que fica no agente) e mantém estas
    funções puras em relação à escolha de provider.
"""
import re
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from app.llm.provider import LLMProvider

log = structlog.get_logger()

SP_TZ = ZoneInfo("America/Sao_Paulo")

_DIAS_PT = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


def _fmt_sp(iso_utc: str | None) -> str:
    """ISO UTC → 'quinta 25/06 10:17' em America/Sao_Paulo. Vazio se inválido."""
    if not iso_utc:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso_utc).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        loc = dt.astimezone(SP_TZ)
        return f"{_DIAS_PT[loc.weekday()]} {loc.strftime('%d/%m %H:%M')}"
    except (ValueError, TypeError):
        return str(iso_utc)[:16].replace("T", " ")


# ── System Prompt (padrão do dia — Mensagem 3) ────────────────────────────────

SYSTEM_PROMPT_DONA = (
    "Você é uma analista gerencial de imobiliária. Com base nos dados de atendimento "
    "do dia, identifique UM padrão genuíno e relevante para a gestora.\n\n"
    "REGRAS:\n"
    "- Máximo 2 frases\n"
    "- Só inclua se há padrão real — não invente insight vago\n"
    "- Tom direto, dado-driven, sem elogio vazio\n"
    "- Se não há padrão genuíno nos dados, responda exatamente: NENHUM_PADRAO\n"
    "OUTPUT: Telegram Markdown válido. Use *negrito* para destaques. Sem headers (#)."
)

# ── Narrative system prompt (Onda 1.7 — por lead) ────────────────────────────

_NARRATIVE_SYSTEM = (
    "Você é uma analista de relacionamento que escreve briefings NARRATIVOS pra "
    "Pessoa Dona — sócia da imobiliária — ler à noite. "
    "Estilo: jornalismo de negócios, não relatório robótico.\n\n"
    "REGRAS DE NARRATIVA:\n"
    "- 2-3 parágrafos curtos por lead\n"
    "- O corpo é a CONVERSA: o que o lead falou, o que a corretora ou o bot falou, "
    "a última fala, a situação que isso cria\n"
    "- Conta a história em ordem cronológica (o que rolou primeiro, depois, depois)\n"
    "- Cita TRECHOS REAIS da conversa entre aspas — APENAS quando a frase importa "
    "pro insight (nunca fala genérica)\n"
    "- Mostra DETALHES contextuais que importam (horário, valor, bairro, número de "
    "mensagens trocadas)\n"
    "- Termina com a SITUAÇÃO atual ('visita marcada amanhã 10h', 'aguardando "
    "holerite', 'sem retorno desde ontem')\n"
    "- NÃO inclui 'sugestão' nem 'recomendação' — só conta o que aconteceu\n"
    "- Tom: profissional mas humano, levemente investigativo\n"
    "- NUNCA inventa fatos que não estão na conversa\n\n"
    "MANCHETE = o que a CONVERSA mostra (pedido do lead, resposta, pendência). "
    "É PROIBIDO abrir com etapa de CRM ('no Follow-up', 'está em Em atendimento', "
    "'lead da Pessoa11 no Follow-up'). "
    "É PROIBIDO 'automação funcionou' / 'a automação mandou' como corpo — se só "
    "houve mensagem automática, CITE O TEXTO da mensagem. "
    "Etapa de CRM, se aparecer, é UMA linha no FINAL, depois da conversa, no "
    "formato: _card em Follow-up_\n\n"
    "EVITAR ABSOLUTAMENTE:\n"
    "- Bullets ou listas\n"
    "- Linguagem de relatório ('foi identificado que', 'observou-se')\n"
    "- Frases de coach ('vale fechar o loop', 'oportunidade de')\n"
    "- Sugestões de ação ('💡 sugestão')\n"
    "- Análise de gap fora da história ('o gap foi X')\n"
    "- Manchete de etapa/automação no lugar da conversa"
)

# Versão SECA do system — usada só na REGERAÇÃO, quando a manchete da primeira
# tentativa veio com etapa de CRM ou resumo de automação.
_NARRATIVE_SYSTEM_SECO = (
    "Você escreve o briefing narrativo de UM lead pra Pessoa Dona.\n"
    "MANCHETE (1ª linha) = o que a CONVERSA mostra. É PROIBIDO manchete com "
    "etapa de CRM ('no Follow-up', 'Em atendimento', 'Novo', 'Nutrição') ou com "
    "'automação funcionou' / 'a automação mandou'.\n"
    "Cite o TEXTO real das mensagens entre aspas — inclusive quando a única "
    "mensagem foi automática (cite o que o bot mandou, não diga que houve bot).\n"
    "2-3 parágrafos curtos, ordem cronológica, sem bullets e sem sugestão.\n"
    "Etapa de CRM, se houver, só UMA linha no FINAL: _card em X_"
)

# ── Telegram split threshold ──────────────────────────────────────────────────

_NARRATIVE_SPLIT_THRESHOLD = 3500


# ── Conversa / etapa: helpers de leitura do raw_analysis ─────────────────────

_ETAPA_KEYS = ("etapa_nome", "etapa", "etapa_crm", "etapa_kommo")

_MSGS_KEYS = ("conversa", "mensagens")

_FROM_LEAD = {"lead", "cliente", "contato", "contact", "them", "remoto", "in"}
_FROM_BOT = {"bot", "automacao", "automação", "corretor", "sistema", "template"}

_TEXTO_KEYS = ("text", "texto", "body", "message", "mensagem", "conteudo")
_AT_KEYS = ("at", "timestamp", "data", "quando", "created_at")

_TEMP_EMOJI = {"quente": "🔥", "morno": "🌡️"}


def _etapa_crm(raw: dict[str, Any]) -> str:
    """Nome da etapa do CRM no raw_analysis. Vazio quando não há.

    `etapa_kommo` só entra quando NÃO é um id numérico (o Inspector às vezes
    grava o id cru da etapa — isso não é nome de etapa e não vai pro texto).
    """
    for key in _ETAPA_KEYS:
        valor = raw.get(key)
        if valor is None:
            continue
        texto = str(valor).strip()
        if not texto:
            continue
        if key == "etapa_kommo" and texto.isdigit():
            continue
        return texto
    return ""


def _msgs_do_raw(raw: dict[str, Any]) -> list[dict[str, Any]]:
    for key in _MSGS_KEYS:
        valor = raw.get(key)
        if isinstance(valor, list):
            msgs = [m for m in valor if isinstance(m, dict)]
            if msgs:
                return msgs
    return []


def _msg_texto(msg: dict[str, Any]) -> str:
    for key in _TEXTO_KEYS:
        valor = msg.get(key)
        if valor:
            return str(valor).strip()
    return ""


def _msg_quando(msg: dict[str, Any]) -> str:
    for key in _AT_KEYS:
        valor = msg.get(key)
        if valor:
            return _fmt_sp(str(valor))
    return ""


def _msg_papel(msg: dict[str, Any]) -> str:
    """LEAD / CORRETORA / BOT — quem falou."""
    if msg.get("is_automacao") or msg.get("automacao"):
        return "BOT"
    origem = str(msg.get("from") or msg.get("de") or msg.get("autor") or "").strip().lower()
    if origem in _FROM_LEAD:
        return "LEAD"
    if origem in _FROM_BOT:
        return "BOT"
    tipo = str(msg.get("type") or msg.get("tipo") or "").strip().lower()
    if tipo in _FROM_BOT:
        return "BOT"
    if tipo in _FROM_LEAD:
        return "LEAD"
    return "CORRETORA"


def format_lead_conversa_bloco(raw: dict[str, Any]) -> str:
    """Bloco de conversa do lead — fonte única pro LLM e pro fallback.

    Quando o raw traz as mensagens (`conversa`/`mensagens`), renderiza em ordem
    cronológica com o texto REAL de cada uma, marcando LEAD / CORRETORA / BOT.
    Mensagem automática entra citada pelo texto que foi enviado — nunca vira a
    frase "automação funcionou".

    Sem lista de mensagens (caso normal do Inspector), monta o bloco a partir de
    `conversa_resumo` + trecho da corretora + última mensagem.
    """
    msgs = _msgs_do_raw(raw)
    linhas: list[str] = []
    for msg in msgs:
        texto = _msg_texto(msg)
        if not texto:
            continue
        papel = _msg_papel(msg)
        quando = _msg_quando(msg)
        prefixo = f"{papel} ({quando})" if quando else papel
        linhas.append(f'{prefixo}: "{texto}"')
    if linhas:
        return "\n".join(linhas)

    conversa_resumo = (raw.get("conversa_resumo") or "").strip()
    o_que_disse = raw.get("o_que_corretora_disse")
    ultima_msg_de = raw.get("ultima_msg_de")
    # E6: ultima_msg_at vem em UTC no raw_analysis — renderiza em hora SP com
    # dia da semana (auditoria pegou 5/5 narrativas com hora UTC e weekday errado).
    ultima_msg_at = _fmt_sp(raw.get("ultima_msg_at"))

    if not conversa_resumo and not o_que_disse:
        return "(sem conversa registrada)"

    partes = [conversa_resumo or "(sem resumo da conversa)"]
    if o_que_disse:
        partes.append(f'\nTrecho da corretora: "{o_que_disse}"')
    if ultima_msg_at and ultima_msg_de:
        partes.append(f"\nÚltima mensagem: {ultima_msg_de} às {ultima_msg_at}")
    return "\n".join(partes)


# ── Trava de manchete (etapa de CRM / automação na 1ª linha) ─────────────────

_HEADLINE_PROIBIDA_RE = re.compile(
    r"automa[cç][aã]o\s+funcionou"
    r"|a\s+automa[cç][aã]o\s+mandou"
    r"|\bfollow[\s\-_]?up\b"
    r"|\bem\s+atendimento\b"
    r"|\bnutri[cç][aã]o\b"
    r"|\b(?:n[oa]|em|na\s+etapa|para\s+[oa]|pr[oa])\s+(?:novo|nova|atendimento)\b"
    r"|\blead\s+d[ao]\s+\S+\s+no\b",
    re.IGNORECASE,
)


def _primeira_linha(texto: str) -> str:
    for linha in (texto or "").splitlines():
        if linha.strip():
            return linha.strip()
    return ""


def _headline_proibida(texto: str) -> bool:
    """True quando a MANCHETE (1ª linha não vazia) é etapa de CRM ou automação.

    Só olha a manchete de propósito: citar a etapa no rodapé (`_card em X_`) é
    o formato correto e não pode ser rejeitado.
    """
    return bool(_HEADLINE_PROIBIDA_RE.search(_primeira_linha(texto)))


def _linha_manchete_conversa(raw: dict[str, Any]) -> str:
    """Uma linha do que a CONVERSA mostra — nunca a etapa do CRM."""
    resumo = (raw.get("conversa_resumo") or "").strip()
    if resumo:
        linha = resumo.splitlines()[0].strip()
    else:
        msgs = _msgs_do_raw(raw)
        do_lead = [m for m in msgs if _msg_papel(m) == "LEAD" and _msg_texto(m)]
        fonte = do_lead or [m for m in msgs if _msg_texto(m)]
        linha = _msg_texto(fonte[0]) if fonte else ""
    if not linha:
        linha = (raw.get("o_que_corretora_disse") or "").strip()
    if not linha:
        return "sem conversa registrada no dia"
    if len(linha) > 120:
        linha = linha[:117].rstrip() + "..."
    return linha


def _fallback_narrativa(raw: dict[str, Any]) -> str:
    """Narrativa determinística — usada quando o LLM insiste na manchete errada.

    Manchete = o que a conversa mostra. Corpo = a conversa citada. Etapa, se
    houver, só numa linha no final.
    """
    nome_lead = raw.get("lead_name") or "?"
    nome_corretora = raw.get("corretora") or "a corretora"
    emoji = _TEMP_EMOJI.get(str(raw.get("temperatura") or "").strip().lower(), "📋")

    partes = [
        f"{emoji} *{nome_lead}* · {nome_corretora} — {_linha_manchete_conversa(raw)}",
        format_lead_conversa_bloco(raw),
    ]
    etapa = _etapa_crm(raw)
    if etapa:
        partes.append(f"_card em {etapa}_")
    return "\n\n".join(partes)


# ── Narrative generation (Onda 1.7) ──────────────────────────────────────────

def generate_lead_narrative(
    raw: dict[str, Any],
    provider: LLMProvider,
) -> tuple[str, dict[str, int]]:
    """
    Gera narrativa jornalística de 2-3 parágrafos para um lead via Sonnet.

    O `provider` é injetado explicitamente (sem default) — quem resolve o
    provider (e é patchável nos testes) é o agente wrapper.

    Returns:
        (narrative_text, {"input": tokens_in, "output": tokens_out})
    """
    nome_lead = raw.get("lead_name") or "?"
    nome_corretora = raw.get("corretora") or "a corretora"
    temperatura = raw.get("temperatura") or "desconhecida"
    status = raw.get("status") or "?"

    conversa_formatada = format_lead_conversa_bloco(raw)

    # A CHAVE segue `tarefas_kommo_atrasadas` (é o nome que o Inspector grava;
    # renomear mexeria no pipeline inteiro). O RÓTULO que a Dona lê não cita
    # mais o Kommo — ele saiu de operação em 31/07.
    tarefas = raw.get("tarefas_kommo_atrasadas") or []
    kommo_ctx = (
        f"\n\nTarefas atrasadas: {', '.join(tarefas)}"
        if tarefas else ""
    )

    gap = raw.get("gap_identificado")
    interno_ctx = (
        f"\n\nContexto adicional (use APENAS pra enriquecer a história, NÃO copie):\n"
        f"Situação identificada: {gap}"
        if gap else ""
    )

    etapa_ctx = ""
    etapa = _etapa_crm(raw)
    if etapa:
        etapa_ctx = (
            f"\n\nEtapa no CRM (NÃO use como manchete; no máximo UMA linha no "
            f"final, depois da conversa, no formato _card em {etapa}_): {etapa}"
        )

    user_prompt = (
        f"Lead: {nome_lead}\n"
        f"Corretora: {nome_corretora}\n"
        f"Temperatura: {temperatura}\n"
        f"Status: {status}\n\n"
        f"Conversa do dia:\n{conversa_formatada}"
        f"{kommo_ctx}"
        f"{interno_ctx}"
        f"{etapa_ctx}\n\n"
        "OUTPUT FORMAT:\n"
        f"[emoji_temperatura] *{nome_lead}* · {nome_corretora} — [o que a CONVERSA "
        "mostra agora em uma linha. PROIBIDO: nome de etapa CRM, 'no Follow-up', "
        "'automação funcionou']\n\n"
        "[parágrafo 1: o que o LEAD falou — cite]\n\n"
        "[parágrafo 2: o que a corretora OU o bot falou — cite]\n\n"
        "[parágrafo 3 — opcional: última fala / pendência]\n\n"
        "[só se houver etapa no input, UMA linha no final:] _card em {etapa}_\n\n"
        "EXEMPLO DE TOM (inspiração de estilo, NÃO copie estrutura literal):\n\n"
        "🔥 *Fulano* · Pessoa3 — visita confirmada segunda 10h, Bairro X\n\n"
        "Lead chegou pela campanha do Facebook às 14h. Pediu pra ver 'lançamento E pronto' "
        "na Bairro X — Pessoa3 mandou 3 opções com valores entre R$280k e R$420k, e ele "
        "engajou rápido em uma das de R$340k.\n\n"
        "Trocaram 11 mensagens em 40 minutos. Ele respondeu 'me interessei muito' e perguntou "
        "se daria pra ver pessoalmente 'antes do fim de semana'. Pessoa3 ofereceu segunda às "
        "10h, ele topou. Visita confirmada, endereço enviado.\n\n"
        "Detalhe importante: Fulano é evangélico, primeira compra, esposa não foi "
        "mencionada.\n\n"
        "_card em Visita marcada_"
    )

    text, usage = provider.complete_text(_NARRATIVE_SYSTEM, user_prompt)
    tokens = {"input": usage.input_tokens, "output": usage.output_tokens}

    # Trava dura: manchete de etapa de CRM / automação não vai pra Dona.
    if _headline_proibida(text):
        log.warning(
            "narrative_headline_rejeitada",
            lead=nome_lead,
            corretora=nome_corretora,
            headline=_primeira_linha(text),
        )
        text_retry, usage_retry = provider.complete_text(
            _NARRATIVE_SYSTEM_SECO, user_prompt
        )
        tokens["input"] += usage_retry.input_tokens
        tokens["output"] += usage_retry.output_tokens
        if _headline_proibida(text_retry):
            log.warning(
                "narrative_fallback_deterministico",
                lead=nome_lead,
                headline=_primeira_linha(text_retry),
            )
            text = _fallback_narrativa(raw)
        else:
            log.info("narrative_regenerada", lead=nome_lead)
            text = text_retry

    log.info(
        "narrative_lead_generated",
        lead=nome_lead,
        corretora=nome_corretora,
        tokens_in=tokens["input"],
        tokens_out=tokens["output"],
    )
    return text.strip(), tokens


def generate_padrao_do_dia(
    context: dict[str, Any],
    provider: LLMProvider,
) -> tuple[str | None, dict[str, int]]:
    """
    Gera insight de padrão do dia em 1-2 frases.

    `provider` injetado explicitamente (ver generate_lead_narrative).

    Returns:
        (padrao_text | None, {"input": tokens_in, "output": tokens_out})
        None quando não há padrão genuíno identificado.
    """
    totais = context.get("totais_globais", {})
    perf = context.get("performance_por_corretora", [])
    top_5 = context.get("top_5_leads_detalhados", [])

    gaps = [lg.get("gap") for lg in top_5 if lg.get("gap")]

    perf_lines = []
    for p in perf:
        if not p.get("is_offline"):
            perf_lines.append(
                f"- {p['nome']}: {p['contatos_unicos']} contatos, "
                f"{p['taxa_resposta_pct']}% resposta, "
                f"{p['quentes']}🔥 {p['mornos_com_gap']}🌡️"
            )

    user_prompt = (
        f"Dia: {context.get('data', '?')}\n"
        f"Leads quentes: {totais.get('leads_quentes', 0)}, "
        f"mornos com gap: {totais.get('leads_mornos_com_gap', 0)}\n\n"
        + ("Performance por corretora:\n" + "\n".join(perf_lines) if perf_lines else "")
        + (f"\n\nGaps do dia: {'; '.join(gaps[:3])}" if gaps else "")
    )

    text, usage = provider.complete_text(SYSTEM_PROMPT_DONA, user_prompt)
    tokens = {"input": usage.input_tokens, "output": usage.output_tokens}

    cleaned = text.strip()
    if not cleaned or "NENHUM_PADRAO" in cleaned:
        log.info("padrao_nao_identificado")
        return None, tokens

    log.info("padrao_gerado", tokens_in=usage.input_tokens, tokens_out=usage.output_tokens)
    return cleaned, tokens


def build_narratives_messages(
    narratives: list[dict[str, Any]],
    data: str,
) -> list[str]:
    """
    Formata narrativas em uma ou mais mensagens Telegram (threshold: 3500 chars).
    Leads nunca são cortados no meio. Separador --- entre leads da mesma seção.
    """
    quentes = [n for n in narratives if n.get("temperatura") == "quente"]
    mornos = [n for n in narratives if n.get("temperatura") != "quente"]

    if not quentes and not mornos:
        return []

    header = f"🔍 *ANÁLISE DOS DESTAQUES — {data}*"

    # Build ordered entries: ("fixed", text) for headers, ("lead", text) for narratives
    entries: list[tuple[str, str]] = [("fixed", header)]
    if quentes:
        entries.append(("fixed", "🔥 *LEADS QUENTES*"))
        for n in quentes:
            entries.append(("lead", n["narrative"]))
    if mornos:
        entries.append(("fixed", "🌡️ *MORNOS COM OPORTUNIDADE*"))
        for n in mornos:
            entries.append(("lead", n["narrative"]))

    def _render(ents: list[tuple[str, str]]) -> str:
        parts: list[str] = []
        prev_lead = False
        for tag, text in ents:
            if tag == "lead" and prev_lead:
                parts.append("---")
            parts.append(text)
            prev_lead = tag == "lead"
        return "\n\n".join(parts)

    full = _render(entries)
    if len(full) <= _NARRATIVE_SPLIT_THRESHOLD:
        return [full]

    # Greedy packing: section headers travel with the next lead
    messages: list[str] = []
    current: list[tuple[str, str]] = []
    buffered_fixed: list[tuple[str, str]] = []

    for tag, text in entries:
        if tag == "fixed":
            buffered_fixed.append((tag, text))
            continue

        candidate = buffered_fixed + [(tag, text)]
        trial = _render(current + candidate)

        if current and len(trial) > _NARRATIVE_SPLIT_THRESHOLD:
            messages.append(_render(current))
            current = candidate
        else:
            current.extend(candidate)

        buffered_fixed = []

    if buffered_fixed:
        current.extend(buffered_fixed)
    if current:
        messages.append(_render(current))

    return messages


def build_padrao_message(padrao_text: str) -> str:
    return f"💡 *PADRÃO QUE NOTEI HOJE*\n\n{padrao_text}"


# ── Dia fraco ─────────────────────────────────────────────────────────────────

def is_dia_fraco(context: dict[str, Any]) -> bool:
    totais = context["totais_globais"]
    return totais["leads_quentes"] == 0 and totais["leads_mornos_com_gap"] == 0


def _linha_robo(d: dict[str, Any] | None) -> str | None:
    """Placar das mensagens automáticas (disparo, régua, nutrição, carrossel, Pessoa).

    Fica fora de "msgs enviadas pelo time" de propósito: sai pela linha da
    corretora, mas não é ela. Lidas só aparece quando há: a maioria dos
    contatos desliga a confirmação de leitura, e "0 lidas" lê como problema.
    """
    if not d or not d.get("enviados"):
        return None
    partes = [
        f"{d['enviados']} automática(s)",
        f"{d.get('entregues', 0)} entregue(s)",
    ]
    if d.get("lidos"):
        partes.append(f"{d['lidos']} lida(s)")
    if d.get("falhas"):
        partes.append(f"{d['falhas']} falha(s)")
    partes.append(f"💬 {d.get('responderam', 0)} responderam")
    return "🤖 " + " · ".join(partes)


# ── Mensagem 1: métricas (determinístico, sem LLM) ────────────────────────────

def format_msg1_metrics(context: dict[str, Any], dia_fraco: bool = False) -> str:
    """Constrói Mensagem 1 — cabeçalho + totais WhatsApp + performance + alertas."""
    totais = context["totais_globais"]
    performance = context["performance_por_corretora"]
    alertas = context["alertas_pipeline"]

    envios_crm_time = totais.get("msgs_enviadas_crm_total")
    contatos_crm_time = totais.get("contatos_crm_total")
    disparos: dict[str, dict[str, Any]] = context.get("disparos") or {}
    robo_total = {
        k: sum(int(d.get(k, 0)) for d in disparos.values())
        for k in ("enviados", "entregues", "lidos", "falhas", "responderam")
    }
    linhas = [
        f"📋 *Acme Imóveis — {context['data']}*\n",
        f"📤 {totais['msgs_enviadas_total']} msgs enviadas pelo time"
        + (
            f" · {envios_crm_time} no CRM"
            if envios_crm_time is not None
            else ""
        ),
        f"📥 {totais['msgs_recebidas_total']} msgs recebidas",
        f"👥 {totais['contatos_unicos_total']} contatos atendidos"
        + (
            f" · {contatos_crm_time} no CRM"
            if contatos_crm_time is not None
            else ""
        ),
        f"🔥 {totais['leads_quentes']} quente(s) · 🌡️ {totais['leads_mornos_com_gap']} morno(s) com gap",
    ]
    _robo = _linha_robo(robo_total)
    if _robo:
        linhas.insert(2, _robo)

    if dia_fraco:
        linhas.append("\n✅ Operação tranquila. Sem leads quentes nem alarmes.")

    if performance:
        linhas.append("\n📊 *PERFORMANCE POR CORRETORA*")
        for p in performance:
            if p.get("is_offline"):
                # P3: não mostrar zeros — mostrar estado real
                linhas.append(f"• {p['nome']} ⚠️ INSTÂNCIA OFFLINE")
                linhas.append("  └ Reconectar QR no Evolution Manager pra retomar análise")
            elif p["contatos_unicos"] == 0:
                linhas.append(f"• {p['nome']}: nenhuma atividade WhatsApp registrada hoje")
            else:
                envios_crm = p.get("msgs_enviadas_crm")
                contatos_crm = p.get("contatos_crm")
                linhas.append(f"• {p['nome']}")
                if envios_crm is not None:
                    fora = max(0, int(p["msgs_enviadas"]) - int(envios_crm))
                    linhas.append(
                        f"  └ 📤 {p['msgs_enviadas']} envio(s) · "
                        f"{envios_crm} no CRM · {fora} fora"
                    )
                    linhas.append(
                        f"  └ 📥 {p['msgs_recebidas']} recebido(s)"
                    )
                else:
                    linhas.append(
                        f"  └ 📤 {p['msgs_enviadas']} envio(s) · "
                        f"📥 {p['msgs_recebidas']} recebido(s)"
                    )
                if contatos_crm is not None:
                    linhas.append(
                        f"  └ 👥 {p['contatos_unicos']} contato(s) · "
                        f"{contatos_crm} no CRM · "
                        f"💬 {p['leads_responderam']} responderam ({p['taxa_resposta_pct']}%)"
                    )
                else:
                    linhas.append(
                        f"  └ 👥 {p['contatos_unicos']} contato(s) · "
                        f"💬 {p['leads_responderam']} responderam ({p['taxa_resposta_pct']}%)"
                    )
                linhas.append(
                    f"  └ {p['quentes']}🔥 · {p['mornos_com_gap']}🌡️ · {p['tarefas_atrasadas']}⏰"
                )
                if p.get("audio_pode_faltar"):
                    linhas.append(
                        "  └ ⚠️ áudios podem não estar contabilizados (canal Cloud)"
                    )
            _robo_c = _linha_robo(disparos.get(p["nome"]))
            if _robo_c and not p.get("is_offline"):
                linhas.append(f"  └ {_robo_c}")

    # Bloco de adoção 7d — Onda Consumo Fase 3
    try:
        from app.services.adocao_metricas import metrica_adocao_semana
        adocao = metrica_adocao_semana()
    except Exception:
        log.exception("adocao_metrica_falhou")
        adocao = None

    if adocao and adocao["gaps_gerados"] > 0:
        linhas.append("\n📈 *ADOÇÃO SEMANAL (7d)*")
        linhas.append(
            f"• {adocao['acoes_marcadas']} de {adocao['gaps_gerados']} "
            f"oportunidades atuadas ({adocao['taxa_pct']:.0f}%)"
        )
        top = adocao.get("top_corretora")
        if top and top["acoes"] > 0:
            linhas.append(
                f"• Top em ação: {top['nome']} "
                f"({top['acoes']}/{top['gaps']} = {top['taxa_pct']:.0f}%)"
            )
        # Zeradas com gap (sinaliza time que não tá usando o painel)
        zeradas = [
            p for p in adocao["por_corretora"]
            if p["gaps"] > 0 and p["acoes"] == 0
        ]
        if zeradas:
            nomes = ", ".join(p["nome"] for p in zeradas)
            linhas.append(f"• Sem nenhuma ação: {nomes}")

    if alertas:
        linhas.append("\n🚨 *ALERTAS DE PIPELINE*")
        for alerta in alertas:
            linhas.append(f"• {alerta}")

    return "\n".join(linhas)


def format_briefing_mini(context: dict[str, Any]) -> str:
    """Template MINI para dia fraco — wrapper de format_msg1_metrics."""
    return format_msg1_metrics(context, dia_fraco=True)


# ── Formatação rica via LLM (dia rico — Onda 1.7) ────────────────────────────

def format_briefing_rich(
    context: dict[str, Any],
    provider: LLMProvider,
) -> tuple[list[str], dict[str, int]]:
    """
    Gera 1-N mensagens Telegram ricas: narrativas por lead + padrão separado.

    `provider` injetado explicitamente — o agente wrapper resolve/patcha.

    Returns:
        (msgs_list, {"input": total_tokens_in, "output": total_tokens_out})
        msgs_list pode ter 1 (tudo cabe) a N mensagens (split por threshold).
        Última mensagem é o padrão do dia (se houver).
    """
    token_stats: dict[str, int] = {"input": 0, "output": 0}
    top_5 = context.get("top_5_leads_detalhados", [])
    data = context.get("data", "")

    # 1. Generate per-lead narratives
    narratives: list[dict[str, Any]] = []
    for lead_entry in top_5:
        raw = lead_entry.get("raw_analysis") or {}
        t0 = time.monotonic()
        narrative_text, tok = generate_lead_narrative(raw, provider=provider)
        elapsed = time.monotonic() - t0
        token_stats["input"] += tok["input"]
        token_stats["output"] += tok["output"]
        log.info(
            "narrative_generated",
            lead=lead_entry.get("nome_lead"),
            elapsed_s=round(elapsed, 1),
        )
        narratives.append({
            "nome_lead": lead_entry.get("nome_lead", "?"),
            "temperatura": lead_entry.get("temperatura", "frio"),
            "narrative": narrative_text,
        })

    # 2. Build narrative messages (may split)
    msgs = build_narratives_messages(narratives, data)

    # 3. Generate padrão do dia (separate message)
    padrao_text, tok_p = generate_padrao_do_dia(context, provider=provider)
    token_stats["input"] += tok_p["input"]
    token_stats["output"] += tok_p["output"]
    if padrao_text:
        msgs.append(build_padrao_message(padrao_text))

    return msgs, token_stats
