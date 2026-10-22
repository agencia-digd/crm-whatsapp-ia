"""
Agente Assistente — briefing gerencial diário para Pessoa Dona via Telegram.

Arquitetura de 3 camadas:
  1. build_dona_context(analyses, whatsapp_metrics) — determinístico, sem LLM, testável
  2. run_daily_briefing(dry_run)  — orquestra: fetch → metrics → format → send → mark
  3. build_agente_assistente(db)     — Agno Agent para registro no AgentOS

DIA FRACO (sem quentes nem mornos com gap):
  Template MINI — zero chamadas LLM, custo zero.
DIA RICO:
  ClaudeCliProvider (Sonnet via CLI) formata o briefing a partir do contexto JSON.
"""
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from agno.agent import Agent
from agno.db.postgres import PostgresDb
from agno.models.anthropic import Claude
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_llm_provider, settings
from app.db.models import BriefingSent, Corretora, InternoAcme, LeadAnalysis
from app.db.session import SessionLocal
from app.delivery.dona_formatter import (
    _NARRATIVE_SPLIT_THRESHOLD,
    _NARRATIVE_SYSTEM,
    SYSTEM_PROMPT_DONA,
    _fmt_sp,
    build_narratives_messages as _build_narratives_messages,
    build_padrao_message as _build_padrao_message,
    format_briefing_mini as _format_briefing_mini,
    format_msg1_metrics as _format_msg1_metrics,
    is_dia_fraco as _is_dia_fraco,
)
from app.delivery import dona_formatter as _dona_fmt
from app.llm.provider import LLMProvider
from app.tools.evolution import EvolutionTool
from app.tools.telegram import TelegramTool

log = structlog.get_logger()

SP_TZ = ZoneInfo("America/Sao_Paulo")

_DIAS_PT = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]

# ── Formatação extraída pra app/delivery/dona_formatter.py (Onda 2) ───────────
# Re-exports acima preservam os nomes públicos/privados históricos importados
# por outros módulos e testes (agente segue sendo o entry-point).
# _fmt_sp, SYSTEM_PROMPT_DONA, _NARRATIVE_SYSTEM, _NARRATIVE_SPLIT_THRESHOLD,
# _format_msg1_metrics, _format_briefing_mini, _build_narratives_messages,
# _build_padrao_message, _is_dia_fraco vêm de dona_formatter.


# ── DB helpers ─────────────────────────────────────────────────────────────────

# Janela ancorada (fix E2 · Revisão Fable etapa 4).
# Antes: "analyzed_at >= hoje 00:00 SP" — se o timer atrasar pra depois da
# meia-noite (Persistent catch-up, reboot) a query zera e o resumo sai "💤".
# Agora: janela deslizante de 36h + gate consumed_by_dona=false. Pega atraso de
# timer/reboot sem arrastar a semana velha (análises antigas já foram consumidas
# na noite anterior, então o gate consumed já as exclui).
_ANALYSES_WINDOW_HOURS = 36


def _fetch_today_analyses(session: Session) -> list[Any]:
    """Busca lead_analyses recentes (janela 36h) ainda não consumidas pela Dona.

    A janela é deslizante (não ancorada em 00:00 SP) pra tolerar atraso do timer
    das 20h passar da meia-noite. O gate `consumed_by_dona=false` garante que
    análises já entregues na noite anterior não voltem — protegendo contra
    resumo dobrado mesmo com a janela mais larga.
    """
    window_start_utc = datetime.now(timezone.utc) - timedelta(hours=_ANALYSES_WINDOW_HOURS)
    rows = (
        session.query(LeadAnalysis)
        .filter(
            LeadAnalysis.analyzed_at >= window_start_utc,
            LeadAnalysis.consumed_by_dona == False,  # noqa: E712
        )
        .order_by(LeadAnalysis.analyzed_at.desc())
        .all()
    )
    # Dedup por lead: com Inspector 2x/dia (07:00 + 19:30) o mesmo lead pode ter
    # 2+ análises não-consumidas na janela — contar todas INFLA os totais do
    # resumo (E6 · Revisão Fable: briefing disse 18🔥/69🌡️ com 12/53 leads reais).
    # Fica só a análise MAIS RECENTE de cada lead. As antigas não retornadas
    # ficam consumed=false mesmo — mas envelhecem pra fora da janela de 36h
    # antes do próximo briefing (07:00 de hoje → 20:00 de amanhã = 37h), então
    # não voltam nem dobram.
    vistos: set[str] = set()
    unicos: list[Any] = []
    for r in rows:
        if r.lead_id in vistos:
            continue
        vistos.add(r.lead_id)
        unicos.append(r)
    return unicos


def _mark_consumed_dona(session: Session, analysis_ids: list[int]) -> None:
    """Marca análises como consumidas pelo briefing da Dona."""
    if not analysis_ids:
        return
    session.query(LeadAnalysis).filter(
        LeadAnalysis.id.in_(analysis_ids)
    ).update({"consumed_by_dona": True}, synchronize_session=False)
    session.commit()
    log.info("assistente_marked_consumed", count=len(analysis_ids))


def _mark_consumed_pessoa(session: Session, analysis_ids: list[int]) -> None:
    """Marca análises como consumidas pelo briefing da Pessoa (Onda 7 · 3C)."""
    if not analysis_ids:
        return
    session.query(LeadAnalysis).filter(
        LeadAnalysis.id.in_(analysis_ids)
    ).update({"consumed_by_pessoa": True}, synchronize_session=False)
    session.commit()
    log.info("assistente_marked_consumed_pessoa", count=len(analysis_ids))


def _notificar_falha_briefing(destino: str, erro: str) -> None:
    """Wrapper best-effort pra avisar a Dona de falha no envio (fix E1).

    Import tardio + try/except pra NUNCA derrubar o briefing por conta da
    notificação (bridge offline, import error, etc.).
    """
    try:
        from app.tools.notify import notify_briefing_failure
        notify_briefing_failure(destino, erro)
    except Exception:
        log.warning("assistente_notify_falha_indisponivel", destino=destino)


def _already_delivered_today(session: Session, destinatario: str) -> bool:
    """Anti-dobro (fix E1): já houve envio com sucesso pro destinatário HOJE (SP)?

    Idempotência de ENTREGA (não só de banco): mesmo que as flags consumed_by_*
    protejam o conteúdo, um re-run manual/timer duplicado no mesmo dia reenviaria
    a Msg 1 determinística. `briefings_sent` é a fonte de verdade de entrega.

    `destinatario` é o valor gravado em briefings_sent: "dona" (Telegram) ou
    "pessoa" (WhatsApp). `sent_at` é gravado como UTC naive (server_default now()),
    então comparamos contra o começo do dia SP convertido pra UTC naive.

    Filtra `is_test.is_(False)`: rodadas de dry-run/teste/smoke gravam registros
    com is_test=True e NUNCA contam como entrega real — senão bloqueariam o
    envio real do dia (incidente do briefing das 20h não entregue à Dona).
    """
    today_start_sp = datetime.now(SP_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc_naive = today_start_sp.astimezone(timezone.utc).replace(tzinfo=None)
    row = (
        session.query(BriefingSent.id)
        .filter(
            BriefingSent.destinatario == destinatario,
            BriefingSent.success.is_(True),
            BriefingSent.is_test.is_(False),
            BriefingSent.sent_at >= today_start_utc_naive,
        )
        .first()
    )
    return row is not None


def _already_sent_contents_today(session: Session, destinatario: str) -> set[str]:
    """Conjunto de `conteudo` já ENTREGUE com sucesso HOJE (SP) pro destinatário.

    Idempotência POR MENSAGEM do briefing multi-mensagem (P0). O briefing da Dona
    sai em N mensagens (Msg 1 métricas + narrativas + padrão). O gate binário
    `_already_delivered_today` sozinho tem dois furos num re-run do mesmo dia:

      • DOBRO: se por algum motivo o gate não pegar (ex.: primeira msg gravou mas
        a leitura do gate correu antes do commit), reenviaria tudo.
      • NARRATIVA SUMIDA: se o run anterior mandou a Msg 1 mas crashou antes das
        narrativas, o gate binário vê "já entregue hoje" e PULA o resto — a
        narrativa nunca chega.

    Comparar o `conteudo` exato de cada mensagem contra o que já saiu hoje resolve
    os dois: reenviamos só o que falta, nunca o que já foi. Msg 1 é
    determinística e as narrativas são estáveis dentro do mesmo contexto do dia,
    então o match por conteúdo é confiável.

    Filtra `is_test=False` (rodadas de dry-run/teste não contam como entrega real,
    mesmo padrão de `_already_delivered_today`).
    """
    today_start_sp = datetime.now(SP_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc_naive = today_start_sp.astimezone(timezone.utc).replace(tzinfo=None)
    rows = (
        session.query(BriefingSent.conteudo)
        .filter(
            BriefingSent.destinatario == destinatario,
            BriefingSent.success.is_(True),
            BriefingSent.is_test.is_(False),
            BriefingSent.sent_at >= today_start_utc_naive,
        )
        .all()
    )
    return {r[0] for r in rows if r[0] is not None}


def _fetch_corretoras_with_evolution(session: Session) -> list[dict[str, Any]]:
    """Fetches active corretoras with evolution data for WhatsApp metrics."""
    corretoras = (
        session.query(Corretora)
        .filter(Corretora.ativo == True)  # noqa: E712
        .all()
    )
    return [
        {
            "nome": c.nome,
            "telefone": c.telefone,
            "evolution_instance": c.evolution_instance,
            "evolution_connected_since": c.evolution_connected_since,
            # `canal` decide a fonte da conversa ('cloud' = tabela, 'evolution' =
            # instância). Sem ele aqui, fetch_whatsapp_metrics caía no nome
            # cravado da Pessoa12 e marcava as outras Cloud como offline (23/09).
            "canal": c.canal,
        }
        for c in corretoras
    ]


def _fetch_internal_phones(session: Session) -> list[str]:
    """Retorna lista de telefones ativos de internos_acme."""
    rows = (
        session.query(InternoAcme.telefone_normalizado)
        .filter(InternoAcme.ativa == True)  # noqa: E712
        .all()
    )
    return [r.telefone_normalizado for r in rows]


# ── Narrative generation (Onda 1.7) — wrappers finos p/ delivery ──────────────
# A montagem do texto foi pra app/delivery/dona_formatter.py (Onda 2). Aqui
# ficam só os wrappers que resolvem o provider (via get_llm_provider deste
# módulo — ponto de patch dos testes) e delegam a formatação.

def generate_lead_narrative(
    raw: dict[str, Any],
    provider: LLMProvider | None = None,
) -> tuple[str, dict[str, int]]:
    """
    Gera narrativa jornalística de 2-3 parágrafos para um lead via Sonnet.

    Returns:
        (narrative_text, {"input": tokens_in, "output": tokens_out})
    """
    if provider is None:
        provider = get_llm_provider()
    return _dona_fmt.generate_lead_narrative(raw, provider=provider)


def generate_padrao_do_dia(
    context: dict[str, Any],
    provider: LLMProvider | None = None,
) -> tuple[str | None, dict[str, int]]:
    """
    Gera insight de padrão do dia em 1-2 frases.

    Returns:
        (padrao_text | None, {"input": tokens_in, "output": tokens_out})
        None quando não há padrão genuíno identificado.
    """
    if provider is None:
        provider = get_llm_provider()
    return _dona_fmt.generate_padrao_do_dia(context, provider=provider)


# ── Snapshot reader (Onda 7.5) ────────────────────────────────────────────────

def ler_snapshot_com_media(
    data_alvo: "date | None" = None,
    conn=None,
) -> list[dict[str, Any]]:
    """
    Lê snapshots_corretora para data_alvo com médias móveis de 14 dias.

    Retorna lista de dicts por corretora com:
      - instance, corretora_nome, envios, recebidos, taxa_resposta
      - media_envios_14d, media_recebidos_14d, media_taxa_14d

    Usado pela Assistente em vez de fetch_whatsapp_metrics() quando o snapshot
    do Inspector já foi gravado (Onda 7.5 — Inspector vira fonte única).

    Args:
        data_alvo: data alvo (default: hoje no fuso SP)
        conn: não usado — mantido por compatibilidade de assinatura
    """
    from datetime import date as _date
    from zoneinfo import ZoneInfo as _ZI

    if data_alvo is None:
        data_alvo = datetime.now(_ZI("America/Sao_Paulo")).date()

    with SessionLocal() as session:
        rows = session.execute(
            text("""
                WITH historico AS (
                    SELECT
                        sc.data,
                        sc.instance,
                        sc.corretora_nome,
                        sc.envios,
                        sc.recebidos,
                        sc.contatos,
                        sc.responderam,
                        sc.taxa_resposta,
                        sc.envios_crm,
                        sc.recebidos_crm,
                        sc.contatos_crm,
                        sc.responderam_crm,
                        sc.taxa_resposta_crm,
                        AVG(sc.envios) OVER (
                            PARTITION BY sc.instance
                            ORDER BY sc.data
                            ROWS BETWEEN 14 PRECEDING AND 1 PRECEDING
                        ) AS media_envios_14d,
                        AVG(sc.recebidos) OVER (
                            PARTITION BY sc.instance
                            ORDER BY sc.data
                            ROWS BETWEEN 14 PRECEDING AND 1 PRECEDING
                        ) AS media_recebidos_14d,
                        AVG(sc.taxa_resposta) OVER (
                            PARTITION BY sc.instance
                            ORDER BY sc.data
                            ROWS BETWEEN 14 PRECEDING AND 1 PRECEDING
                        ) AS media_taxa_14d
                    FROM snapshots_corretora sc
                    WHERE sc.data <= :data_alvo
                )
                SELECT * FROM historico WHERE data = :data_alvo
                ORDER BY instance
            """),
            {"data_alvo": data_alvo},
        ).fetchall()

    result = []
    for r in rows:
        result.append({
            "instance": r.instance,
            "corretora_nome": r.corretora_nome,
            "envios": int(r.envios or 0),
            "recebidos": int(r.recebidos or 0),
            "contatos": int(r.contatos or 0),
            "responderam": int(r.responderam or 0),
            "taxa_resposta": float(r.taxa_resposta or 0),
            "envios_crm": int(r.envios_crm or 0),
            "recebidos_crm": int(r.recebidos_crm or 0),
            "contatos_crm": int(r.contatos_crm or 0),
            "responderam_crm": int(r.responderam_crm or 0),
            "taxa_resposta_crm": float(r.taxa_resposta_crm or 0),
            "media_envios_14d": round(float(r.media_envios_14d), 1) if r.media_envios_14d else None,
            "media_recebidos_14d": round(float(r.media_recebidos_14d), 1) if r.media_recebidos_14d else None,
            "media_taxa_14d": round(float(r.media_taxa_14d), 1) if r.media_taxa_14d else None,
        })

    log.info("ler_snapshot_com_media", data=str(data_alvo), n_corretoras=len(result))
    return result


def _snapshot_para_whatsapp_metrics(
    snapshot_rows: list[dict[str, Any]],
    corretoras: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """
    Converte rows de ler_snapshot_com_media() para o formato de whatsapp_metrics.

    Preserva compatibilidade com build_dona_context() que espera:
      dict[corretora_nome → {msgs_enviadas, msgs_recebidas, contatos_unicos,
                              leads_responderam, taxa_resposta_pct, is_offline,
                              instance_state, media_envios_14d, ...}]
    """
    # Build lookup por instance → nome (de corretoras)
    instance_nome = {
        c["evolution_instance"]: c["nome"]
        for c in corretoras
        if c.get("evolution_instance")
    }

    result: dict[str, dict[str, Any]] = {}
    for r in snapshot_rows:
        nome = instance_nome.get(r["instance"], r["corretora_nome"])
        taxa = r["taxa_resposta"]
        taxa_crm = r.get("taxa_resposta_crm") or 0
        # Preferir contagem real de responderam (gravada no snapshot); fallback % 
        responderam = r.get("responderam")
        if responderam is None:
            responderam = round(r["contatos"] * taxa / 100) if r["contatos"] > 0 else 0
        result[nome] = {
            "msgs_enviadas": r["envios"],
            "msgs_recebidas": r["recebidos"],
            "contatos_unicos": r["contatos"],
            "leads_responderam": int(responderam),
            "taxa_resposta_pct": round(taxa),
            "msgs_enviadas_crm": r.get("envios_crm", 0),
            "msgs_recebidas_crm": r.get("recebidos_crm", 0),
            "contatos_crm": r.get("contatos_crm", 0),
            "leads_responderam_crm": r.get("responderam_crm", 0),
            "taxa_resposta_crm_pct": round(taxa_crm),
            "is_offline": False,
            "instance_state": "open",
            "media_envios_14d": r.get("media_envios_14d"),
            "media_recebidos_14d": r.get("media_recebidos_14d"),
            "media_taxa_14d": r.get("media_taxa_14d"),
        }

    # Garante corretoras sem snapshot aparecem com zeros
    for c in corretoras:
        nome = c["nome"]
        if nome not in result:
            result[nome] = _empty_whatsapp_metrics()

    return result


def _snapshot_disponivel_hoje() -> bool:
    """True se snapshots_corretora já tem linha pra hoje (Inspector rodou)."""
    from zoneinfo import ZoneInfo as _ZI
    alvo = datetime.now(_ZI("America/Sao_Paulo")).date()
    try:
        with SessionLocal() as session:
            n = session.execute(
                text("SELECT COUNT(*) FROM snapshots_corretora WHERE data = :d"),
                {"d": alvo},
            ).scalar()
        return int(n or 0) > 0
    except Exception:
        return False


# ── WhatsApp metrics ──────────────────────────────────────────────────────────

def _empty_whatsapp_metrics() -> dict[str, Any]:
    return {
        "msgs_enviadas": 0,
        "msgs_recebidas": 0,
        "contatos_unicos": 0,
        "leads_responderam": 0,
        "taxa_resposta_pct": 0,
        "msgs_enviadas_crm": 0,
        "msgs_recebidas_crm": 0,
        "contatos_crm": 0,
        "leads_responderam_crm": 0,
        "taxa_resposta_crm_pct": 0,
        "is_offline": False,
        "instance_state": "open",
    }


def _offline_whatsapp_metrics(state: str) -> dict[str, Any]:
    """Métricas zeradas + marcador de instância offline."""
    return {
        "msgs_enviadas": 0,
        "msgs_recebidas": 0,
        "contatos_unicos": 0,
        "leads_responderam": 0,
        "taxa_resposta_pct": 0,
        "msgs_enviadas_crm": 0,
        "msgs_recebidas_crm": 0,
        "contatos_crm": 0,
        "leads_responderam_crm": 0,
        "taxa_resposta_crm_pct": 0,
        "is_offline": True,
        "instance_state": state,
    }


def fetch_whatsapp_metrics(
    corretoras: list[dict[str, Any]],
    *,
    _cutoff_override: datetime | None = None,
    extra_internal_phones: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Busca métricas de atividade WhatsApp por corretora via Postgres (mensagens_whatsapp).

    Janela: dia corrente no fuso SP (meia-noite SP até agora). Se
    evolution_connected_since for de HOJE (corretora reconectou no meio do dia),
    a janela daquela instância começa no connected_since — evita recontar
    conversa re-sincronizada pela Evolution na reconexão (Fix 2).
    Exclui chats internos: telefones em internos_acme E telefones das próprias
    corretoras — chat corretora↔corretora não é lead (Fix 5).
    Mantém 1 chamada Evolution API por corretora apenas para verificar connectionState.

    Args:
        corretoras: lista de dicts {nome, telefone, evolution_instance, evolution_connected_since}
        _cutoff_override: substitui meia-noite SP em testes (não usar em produção)
        extra_internal_phones: mantido por compatibilidade (deprecated — filtro
            lê internos_acme diretamente no SQL agora)

    Returns:
        dict[corretora_nome → {msgs_enviadas, msgs_recebidas, contatos_unicos,
                               leads_responderam, taxa_resposta_pct,
                               is_offline, instance_state}]
    """
    result: dict[str, dict[str, Any]] = {}

    midnight_sp: datetime = _cutoff_override or (
        datetime.now(SP_TZ)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(timezone.utc)
    )
    tomorrow_utc: datetime = midnight_sp + timedelta(days=1)

    instance_to_nome: dict[str, str] = {
        c["evolution_instance"]: c["nome"]
        for c in corretoras
        if c.get("evolution_instance")
    }

    # ── Verifica connectionState (Evolution API — 1 call por corretora) ─────────
    try:
        evolution = EvolutionTool()
    except Exception:
        log.warning("whatsapp_metrics_evolution_init_failed")
        return {c["nome"]: _empty_whatsapp_metrics() for c in corretoras}

    # Canal Cloud API: não é instância Evolution — grava direto no Postgres via
    # os webhooks cloud_*. Não tem connectionState pra checar; as métricas vêm da
    # tabela mensagens_whatsapp como pras demais. Sem esse skip, o status check
    # retorna "close" e o briefing marca a corretora offline (métricas
    # descartadas mesmo com msgs no banco).
    #
    # 23/09/2026 — a fonte da verdade é `corretoras.canal`, criado pra isso na
    # migration 0056 (25/07). Até hoje este trecho comparava com o nome cravado
    # "acme_corretora_a" (código de 03/07, três semanas ANTES da coluna existir).
    # Quando as outras cinco foram pra Cloud — UPDATE feito na mão, sem migration
    # — o banco ficou certo e o código continuou perguntando a pergunta errada:
    # Pessoa1, Pessoa5, Pessoa7, Corretor e Corretor apareciam "INSTÂNCIA OFFLINE" no
    # briefing com as mensagens delas no banco, e a adoção semanal dava 0%.
    # Fallback pelo nome fica por compatibilidade com cadastro sem canal.
    from app.api.cloud_corretora_a import INSTANCE as CLOUD_INSTANCE

    offline_instances: set[str] = set()
    for c in corretoras:
        nome: str = c["nome"]
        instance: str | None = c.get("evolution_instance")
        if not instance:
            result[nome] = _empty_whatsapp_metrics()
            log.info("whatsapp_metrics_no_instance", corretora=nome)
            continue
        if c.get("canal") == "cloud" or instance == CLOUD_INSTANCE:
            # canal Cloud — sempre "online" pra fins de métrica (fonte é o Postgres)
            log.info(
                "whatsapp_metrics_cloud_channel",
                corretora=nome,
                instance=instance,
                canal=c.get("canal"),
            )
            continue
        try:
            state = evolution._client.get_instance_status(instance)
        except Exception:
            log.warning("whatsapp_metrics_instance_status_failed", corretora=nome)
            state = "unknown"
        if state != "open":
            log.warning(
                "instance_offline_metrics_skipped",
                corretora=nome,
                instance=instance,
                state=state,
            )
            result[nome] = _offline_whatsapp_metrics(state)
            offline_instances.add(instance)

    online_instances = [
        c["evolution_instance"]
        for c in corretoras
        if c.get("evolution_instance") and c["evolution_instance"] not in offline_instances
    ]
    if not online_instances:
        return result

    # Fix 2 (perdido na migração Postgres-only, commit 3e45f936): connected_since
    # só restringe quando a corretora (re)conectou HOJE — cutoff efetivo por
    # instância = max(meia-noite SP, evolution_connected_since).
    inst_cutoff: dict[str, datetime] = {}
    for c in corretoras:
        inst = c.get("evolution_instance")
        if not inst:
            continue
        cs: datetime | None = c.get("evolution_connected_since")
        if cs is not None and cs.tzinfo is None:
            cs = cs.replace(tzinfo=timezone.utc)
        inst_cutoff[inst] = cs if (cs is not None and cs > midnight_sp) else midnight_sp
    online_cutoffs = [inst_cutoff[i] for i in online_instances]

    # Fix 5 (mesma regressão): chat corretora↔corretora não é lead — exclui
    # qualquer chat cujo contato seja telefone de corretora cadastrada.
    corretora_phones: list[str] = [
        p
        for p in (
            (c.get("telefone") or "").lstrip("+").replace(" ", "").replace("-", "")
            for c in corretoras
        )
        if p
    ]

    # ── Métricas do dia via Postgres ─────────────────────────────────────────────
    with SessionLocal() as session:
        rows = session.execute(
            text("""
                WITH inst_cutoffs AS (
                    SELECT unnest(CAST(:instances AS text[]))        AS instance,
                           unnest(CAST(:cutoffs   AS timestamptz[])) AS cutoff
                ),
                msgs_today AS (
                    SELECT me.instance, me.remote_jid, me.from_corretora,
                           me.timestamp_evolution,
                           -- escrita por gente (mesma regra do Inspector e da Visão)
                           (me.from_corretora
                            AND COALESCE(me.tipo_mensagem, '') <> 'template'
                            AND COALESCE(me.conteudo_metadata->>'origem', '') = '')
                               AS humana
                    FROM mensagens_whatsapp me
                    JOIN inst_cutoffs ic ON ic.instance = me.instance
                    WHERE me.timestamp_evolution >= ic.cutoff
                      AND me.timestamp_evolution < :tomorrow_utc
                      AND me.remote_jid NOT LIKE '%@g.us'
                      -- Fix 5: chat com telefone de corretora não é lead
                      AND split_part(me.remote_jid, '@', 1)
                          <> ALL(CAST(:corretora_phones AS text[]))
                      AND (me.contato_telefone IS NULL
                           OR (me.contato_telefone NOT IN (
                                   SELECT telefone_normalizado
                                   FROM internos_acme
                                   WHERE ativa = true
                               )
                               AND me.contato_telefone
                                   <> ALL(CAST(:corretora_phones AS text[]))))
                ),
                -- Chats em que a corretora ESCREVEU >= 1 msg hoje
                abordados AS (
                    SELECT instance, remote_jid,
                           MIN(timestamp_evolution) AS first_cor_at
                    FROM msgs_today
                    WHERE humana
                    GROUP BY instance, remote_jid
                ),
                -- Todas as msgs de chats abordados (enviadas + recebidas)
                msgs_abordados AS (
                    SELECT m.instance, m.remote_jid,
                           m.from_corretora, m.humana, m.timestamp_evolution
                    FROM msgs_today m
                    INNER JOIN abordados a
                        ON m.instance = a.instance AND m.remote_jid = a.remote_jid
                ),
                -- Lead respondeu APÓS a primeira msg da corretora
                lead_respondeu AS (
                    SELECT DISTINCT m.instance, m.remote_jid
                    FROM msgs_abordados m
                    JOIN abordados a
                        ON m.instance = a.instance AND m.remote_jid = a.remote_jid
                    WHERE m.from_corretora = false
                      AND m.timestamp_evolution > a.first_cor_at
                )
                SELECT
                    a.instance,
                    COUNT(DISTINCT a.remote_jid)   AS contatos_unicos,
                    COUNT(*) FILTER (WHERE m.humana)                 AS msgs_enviadas,
                    COUNT(*) FILTER (WHERE m.from_corretora = false) AS msgs_recebidas,
                    COUNT(DISTINCT lr.remote_jid)  AS leads_responderam
                FROM abordados a
                JOIN msgs_abordados m
                    ON m.instance = a.instance AND m.remote_jid = a.remote_jid
                LEFT JOIN lead_respondeu lr
                    ON lr.instance = a.instance AND lr.remote_jid = a.remote_jid
                GROUP BY a.instance
            """),
            {
                "tomorrow_utc": tomorrow_utc,
                "instances": online_instances,
                "cutoffs": online_cutoffs,
                "corretora_phones": corretora_phones,
            },
        ).fetchall()

    for r in rows:
        nome = instance_to_nome.get(r.instance, r.instance)
        contatos = int(r.contatos_unicos or 0)
        responderam = int(r.leads_responderam or 0)
        taxa = round(responderam / contatos * 100) if contatos > 0 else 0
        result[nome] = {
            "msgs_enviadas": int(r.msgs_enviadas or 0),
            "msgs_recebidas": int(r.msgs_recebidas or 0),
            "contatos_unicos": contatos,
            "leads_responderam": responderam,
            "taxa_resposta_pct": taxa,
            "is_offline": False,
            "instance_state": "open",
        }
        log.info(
            "whatsapp_metrics_fetched",
            corretora=nome,
            contatos=contatos,
            enviadas=int(r.msgs_enviadas or 0),
            recebidas=int(r.msgs_recebidas or 0),
            responderam=responderam,
        )

    # Instâncias online sem msgs hoje → métricas zeradas (não offline)
    for c in corretoras:
        nome = c["nome"]
        if nome not in result and c.get("evolution_instance") not in offline_instances:
            result[nome] = _empty_whatsapp_metrics()

    # Fallback H1 — canal Cloud (Pessoa12/2591): a Meta às vezes NÃO ecoa os áudios que
    # a corretora manda pelo celular (message_echoes chega text/image, mas áudio
    # some intermitentemente). Marca a corretora pra o briefing avisar que a
    # contagem de envios pode estar subestimada. (Não afeta linhas Evolution.)
    for c in corretoras:
        nome = c["nome"]
        if c.get("evolution_instance") == CLOUD_INSTANCE and nome in result:
            result[nome]["audio_pode_faltar"] = True

    return result


# ── Placar do robô (disparo, régua, nutrição, carrossel, Pessoa) ───────────────
# 09/10/2026: a Dona perguntou se o resumo das 20h "está mandando os disparos".
# Não estava: o template automático entrava somado em "msgs enviadas pelo time"
# (08/10: 138 de 638 — 53 dos 70 da Corretor eram carrossel) e o alerta "pipeline
# morrendo" media resposta a disparo como se fosse trabalho da corretora.
# Agora o envio do time é só o escrito por gente e o robô ganha linha própria.
SQL_DISPAROS_DO_DIA = """
WITH t AS (
    SELECT m.instance, m.remote_jid, m.timestamp_evolution AS ts,
           COALESCE(m.conteudo_metadata->>'delivery_status', '') AS st
      FROM mensagens_whatsapp m
     WHERE m.from_corretora
       AND (COALESCE(m.tipo_mensagem, '') = 'template'
            OR COALESCE(m.conteudo_metadata->>'origem', '') <> '')
       AND m.remote_jid NOT LIKE '%@g.us'
       AND m.instance = ANY(CAST(:instances AS text[]))
       AND m.timestamp_evolution >= :ini AND m.timestamp_evolution < :fim
       AND (m.contato_telefone IS NULL OR m.contato_telefone NOT IN (
                SELECT telefone_normalizado FROM internos_acme WHERE ativa = true))
)
SELECT t.instance,
       COUNT(*)                                             AS enviados,
       COUNT(*) FILTER (WHERE t.st IN ('delivered', 'read')) AS entregues,
       COUNT(*) FILTER (WHERE t.st = 'read')                 AS lidos,
       COUNT(*) FILTER (WHERE t.st = 'failed')               AS falhas,
       COUNT(DISTINCT t.remote_jid) FILTER (WHERE EXISTS (
           SELECT 1 FROM mensagens_whatsapp r
            WHERE r.instance = t.instance AND r.remote_jid = t.remote_jid
              AND NOT r.from_corretora
              AND r.timestamp_evolution > t.ts
              AND r.timestamp_evolution < :fim))               AS responderam
  FROM t
 GROUP BY t.instance
"""


def fetch_disparos_do_dia(
    corretoras: list[dict[str, Any]],
    _agora: datetime | None = None,
) -> dict[str, dict[str, int]]:
    """Mensagens automáticas que saíram pela linha de cada corretora hoje (SP).

    Automática = template OU `conteudo_metadata.origem` preenchida (a regra da
    Visão). Entregue/lido/falha vêm do `delivery_status` gravado pelo webhook
    de status da Meta; "responderam" = contatos distintos que mandaram msg
    depois do disparo, até agora. Só leitura. Em erro devolve {} — o briefing
    sai sem a linha do robô, não deixa de sair.
    """
    agora = _agora or datetime.now(timezone.utc)
    ini = (
        agora.astimezone(SP_TZ)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(timezone.utc)
    )
    instance_to_nome = {
        c["evolution_instance"]: c["nome"]
        for c in corretoras
        if c.get("evolution_instance")
    }
    if not instance_to_nome:
        return {}
    try:
        with SessionLocal() as session:
            rows = session.execute(
                text(SQL_DISPAROS_DO_DIA),
                {"instances": list(instance_to_nome), "ini": ini, "fim": agora},
            ).fetchall()
    except Exception:
        log.exception("disparos_do_dia_falhou")
        return {}
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        out[instance_to_nome.get(r.instance, r.instance)] = {
            "enviados": int(r.enviados or 0),
            "entregues": int(r.entregues or 0),
            "lidos": int(r.lidos or 0),
            "falhas": int(r.falhas or 0),
            "responderam": int(r.responderam or 0),
        }
    return out


# ── Context builder — função pura, sem acesso a DB ────────────────────────────

def build_dona_context(
    analyses: list[Any],
    whatsapp_metrics: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Constrói dict estruturado a partir de analyses e métricas WhatsApp.
    Função pura — sem acesso a DB.

    Args:
        analyses: lista de LeadAnalysis ORM objects (ou mocks com mesma interface)
        whatsapp_metrics: dict[corretora_nome → metrics] de fetch_whatsapp_metrics().
            Se None, métricas de WhatsApp ficam zeradas (útil em testes).

    Returns:
        Dict com totais_globais, performance_por_corretora, top_5_leads_detalhados,
        alertas_pipeline e data.
    """
    wm = whatsapp_metrics or {}
    agora_utc = datetime.now(timezone.utc)
    quatro_horas_atras = agora_utc - timedelta(hours=4)

    # ── Agregar por corretora (dados de lead_analyses) ────────────────────────
    corretoras: dict[str, dict[str, Any]] = {}

    for a in analyses:
        raw = a.raw_analysis or {}
        nome_corretora = raw.get("corretora") or f"id_{a.corretora_id}"

        if nome_corretora not in corretoras:
            corretoras[nome_corretora] = {
                "nome": nome_corretora,
                "quentes": 0,
                "mornos_com_gap": 0,
                "tarefas_atrasadas": 0,
            }
        c = corretoras[nome_corretora]

        temp = a.temperatura or "frio"
        if temp == "quente":
            c["quentes"] += 1
        elif temp == "morno" and a.has_gap:
            c["mornos_com_gap"] += 1

        if (a.status or "") == "atendimento_pendente":
            c["tarefas_atrasadas"] += 1

    # Garantir que corretoras com whatsapp_metrics mas sem análises apareçam
    for nome_wm in wm:
        if nome_wm not in corretoras:
            corretoras[nome_wm] = {
                "nome": nome_wm,
                "quentes": 0,
                "mornos_com_gap": 0,
                "tarefas_atrasadas": 0,
            }

    # ── Totais globais ────────────────────────────────────────────────────────
    total_quentes = sum(c["quentes"] for c in corretoras.values())
    total_mornos_gap = sum(c["mornos_com_gap"] for c in corretoras.values())
    total_tarefas = sum(c["tarefas_atrasadas"] for c in corretoras.values())

    msgs_enviadas_total = sum(m.get("msgs_enviadas", 0) for m in wm.values())
    msgs_recebidas_total = sum(m.get("msgs_recebidas", 0) for m in wm.values())
    contatos_unicos_total = sum(m.get("contatos_unicos", 0) for m in wm.values())
    msgs_enviadas_crm_total = sum(m.get("msgs_enviadas_crm", 0) for m in wm.values())
    contatos_crm_total = sum(m.get("contatos_crm", 0) for m in wm.values())

    # ── Performance por corretora (merge analyses + WhatsApp) ─────────────────
    performance: list[dict[str, Any]] = []
    for nome, c in corretoras.items():
        m = wm.get(nome, _empty_whatsapp_metrics())
        performance.append({
            "nome": nome,
            "msgs_enviadas": m["msgs_enviadas"],
            "msgs_recebidas": m["msgs_recebidas"],
            "contatos_unicos": m["contatos_unicos"],
            "leads_responderam": m["leads_responderam"],
            "taxa_resposta_pct": m["taxa_resposta_pct"],
            "msgs_enviadas_crm": m.get("msgs_enviadas_crm", 0),
            "msgs_recebidas_crm": m.get("msgs_recebidas_crm", 0),
            "contatos_crm": m.get("contatos_crm", 0),
            "leads_responderam_crm": m.get("leads_responderam_crm", 0),
            "taxa_resposta_crm_pct": m.get("taxa_resposta_crm_pct", 0),
            "quentes": c["quentes"],
            "mornos_com_gap": c["mornos_com_gap"],
            "tarefas_atrasadas": c["tarefas_atrasadas"],
            "is_offline": m.get("is_offline", False),
            "instance_state": m.get("instance_state", "open"),
            "audio_pode_faltar": m.get("audio_pode_faltar", False),
            # Onda 7.5: médias móveis do snapshot (None quando não disponível)
            "media_envios_14d": m.get("media_envios_14d"),
            "media_recebidos_14d": m.get("media_recebidos_14d"),
            "media_taxa_14d": m.get("media_taxa_14d"),
        })

    # ── Top 5 leads detalhados ────────────────────────────────────────────────
    # Prioridade: quente > morno com gap; empate: analyzed_at desc
    candidates: list[tuple[int, float, Any, dict[str, Any]]] = []
    for a in analyses:
        raw = a.raw_analysis or {}
        temp = a.temperatura or "frio"
        if temp not in ("quente", "morno"):
            continue
        if temp == "morno" and not a.has_gap:
            continue
        priority = 0 if temp == "quente" else 1
        ts = a.analyzed_at.timestamp() if a.analyzed_at else 0.0
        candidates.append((priority, ts, a, raw))

    candidates.sort(key=lambda x: (x[0], -x[1]))
    top_5: list[dict[str, Any]] = []
    for _, _, a, raw in candidates[:5]:
        top_5.append({
            "nome_lead": raw.get("lead_name") or "?",
            "corretora": raw.get("corretora") or "",
            "temperatura": a.temperatura or "frio",
            "modo": raw.get("analysis_mode") or "",
            "gap": raw.get("gap_identificado"),
            "sugestao": raw.get("abordagem_sugerida"),
            "raw_analysis": raw,
        })

    # ── Alertas pipeline ──────────────────────────────────────────────────────
    alertas: list[str] = []

    for p in performance:
        # P3: instância offline → alerta específico, sem métricas falsas
        if p.get("is_offline"):
            alertas.append(
                f"⚠️ {p['nome']} desconectada — análise indisponível"
            )
            continue  # não gerar alerta de pipeline com dados stale

        if p["contatos_unicos"] > 5 and p["taxa_resposta_pct"] < 25:
            alertas.append(
                f"{p['nome']}: taxa de resposta {p['taxa_resposta_pct']}% "
                f"em {p['contatos_unicos']} contatos — pipeline morrendo"
            )
        if p["tarefas_atrasadas"] >= 3:
            alertas.append(
                f"{p['nome']}: {p['tarefas_atrasadas']} tarefa(s) vencida(s) "
                "sem contato"
            )

    # Leads com última msg do lead >4h sem retorno da corretora
    sem_retorno = 0
    com_conversa = 0
    for a in analyses:
        if (a.status or "") in ("atendido", "sem_resposta"):
            com_conversa += 1
            raw = a.raw_analysis or {}
            if raw.get("ultima_msg_de") == "lead":
                ultima_at_str = raw.get("ultima_msg_at")
                if ultima_at_str:
                    try:
                        ultima_at = datetime.fromisoformat(ultima_at_str)
                        if ultima_at.tzinfo is None:
                            ultima_at = ultima_at.replace(tzinfo=timezone.utc)
                        if ultima_at < quatro_horas_atras:
                            sem_retorno += 1
                    except ValueError:
                        pass

    if com_conversa > 0 and sem_retorno / com_conversa >= 0.30:
        pct = int(sem_retorno / com_conversa * 100)
        alertas.append(
            f"{pct}% dos leads com conversa aguardando retorno há mais de 4h"
        )

    return {
        "data": datetime.now(SP_TZ).strftime("%d/%m/%Y"),
        "totais_globais": {
            "msgs_enviadas_total": msgs_enviadas_total,
            "msgs_recebidas_total": msgs_recebidas_total,
            "contatos_unicos_total": contatos_unicos_total,
            "msgs_enviadas_crm_total": msgs_enviadas_crm_total,
            "contatos_crm_total": contatos_crm_total,
            "leads_quentes": total_quentes,
            "leads_mornos_com_gap": total_mornos_gap,
            "tarefas_atrasadas_total": total_tarefas,
        },
        "performance_por_corretora": performance,
        "top_5_leads_detalhados": top_5,
        "alertas_pipeline": alertas,
    }


# ── Dia fraco / Mensagem 1 / Mini / Rich ─────────────────────────────────────
# Movidos pra app/delivery/dona_formatter.py (Onda 2). _is_dia_fraco,
# _format_msg1_metrics e _format_briefing_mini são re-exportados no topo do
# módulo. _format_briefing_rich vira wrapper fino que resolve o provider (via
# get_llm_provider deste módulo — ponto de patch dos testes) e delega.

def _format_briefing_rich(
    context: dict[str, Any],
) -> tuple[list[str], dict[str, int]]:
    """
    Gera 1-N mensagens Telegram ricas: narrativas por lead + padrão separado.

    Returns:
        (msgs_list, {"input": total_tokens_in, "output": total_tokens_out})
        msgs_list pode ter 1 (tudo cabe) a N mensagens (split por threshold).
        Última mensagem é o padrão do dia (se houver).
    """
    llm = get_llm_provider()
    return _dona_fmt.format_briefing_rich(context, provider=llm)


# ── Runner principal ──────────────────────────────────────────────────────────

_FOOTER_INSPECTOR_PENDENTE = (
    "\n\n🔍 _Análise detalhada dos leads chega às 20h após o Inspector rodar._"
)
_WARNING_EVOLUTION_OFFLINE = (
    "\n\n⚠️ _Métricas WhatsApp indisponíveis hoje (Evolution offline?). "
    "Análise de leads do Inspector preservada._"
)
_WARNING_SEM_ATIVIDADE_WHATSAPP = (
    "\n\n📭 _Sem atividade WhatsApp registrada hoje. "
    "Análise de leads do Inspector preservada._"
)


def _bloco_leitura_ia() -> str:
    """Mensagem do briefing com as conversas do dia que a IA leu e viu fechando."""
    from app.services import leitura_ia

    achados = leitura_ia.procurar(horas=24)
    fortes = [a for a in achados if a.forca == "forte"]
    if not fortes:
        return ""
    linhas = [f"🔥 *{len(fortes)} conversa(s) que a IA leu hoje e viu fechando negócio*", ""]
    for a in fortes[:8]:
        linhas.append(f"• *{a.nome or a.telefone[-4:]}* — está em _{a.etapa or 'sem card'}_")
        if a.trechos and a.trechos[0]:
            linhas.append(f'   "{a.trechos[0]}"')
    if len(fortes) > 8:
        linhas.append(f"\n… e mais {len(fortes) - 8}.")
    linhas.append("\nSe alguma está fechando de verdade, o card precisa subir.")
    return "\n".join(linhas)


def run_daily_briefing(dry_run: bool = True, force_mini: bool = False) -> dict[str, Any]:
    """
    Executa o briefing diário para Dona em 1-N mensagens Telegram.

    4 cenários determinados por (contatos_unicos_total, n_analyses):

      Cenário 1 — tudo zerado (0, 0):
        Mensagem única "💤 Sem atividade".

      Cenário 2 — WhatsApp ativo, Inspector ainda não rodou (>0, 0):
        Mensagem 1 com métricas WhatsApp + footer explicativo.

      Cenário 3 — tudo pronto (>0, >0):
        Mensagem 1 (métricas) + Mensagens ricas (narrativas + padrão, se dia rico).

      Edge — Inspector rodou mas Evolution caiu (0, >0):
        Igual Cenário 3, mas aviso discreto em Mensagem 1.

    Args:
        dry_run: se True, não envia nem marca consumed (default=True)
        force_mini: se True, suprime mensagens ricas (debug)

    Returns:
        dict com briefing_msg1, briefing_msg2 (backward compat = msgs_rich[0]),
        briefing_msgs_rich (list), scenario, leads_processados, tokens,
        elapsed_s, dia_fraco, dry_run
    """
    start = time.monotonic()
    token_stats: dict[str, int] = {"input": 0, "output": 0}

    # ── Step 1: Fetch data from DB ────────────────────────────────────────────
    with SessionLocal() as session:
        analyses = _fetch_today_analyses(session)
        corretoras_data = _fetch_corretoras_with_evolution(session)
        internal_phones = _fetch_internal_phones(session)
        analysis_ids = [a.id for a in analyses if a.id is not None]

    # ── Step 2: Fetch WhatsApp metrics ───────────────────────────────────────
    # Onda 7.5: preferir snapshots gravados pelo Inspector (fonte única).
    # Fallback: query direta em mensagens_whatsapp (comportamento legado).
    _usou_snapshot = _snapshot_disponivel_hoje()
    if _usou_snapshot:
        log.info("assistente_usando_snapshot_diario")
        snapshot_rows = ler_snapshot_com_media()
        whatsapp_metrics = _snapshot_para_whatsapp_metrics(snapshot_rows, corretoras_data)
    else:
        log.info("assistente_fallback_query_direta")
        whatsapp_metrics = fetch_whatsapp_metrics(
            corretoras_data, extra_internal_phones=internal_phones
        )

    # ── Step 3: Build context ─────────────────────────────────────────────────
    context = build_dona_context(analyses, whatsapp_metrics=whatsapp_metrics)
    context["disparos"] = fetch_disparos_do_dia(corretoras_data)
    totais = context["totais_globais"]

    n_analyses = len(analyses)
    contatos_total = totais["contatos_unicos_total"]

    # ── Step 4: Determine scenario ────────────────────────────────────────────
    if contatos_total == 0 and n_analyses == 0:
        scenario = "cenario_1_tudo_zerado"
    elif contatos_total > 0 and n_analyses == 0:
        scenario = "cenario_2_whatsapp_sem_inspector"
    elif contatos_total == 0 and n_analyses > 0:
        scenario = "edge_inspector_sem_whatsapp"
    else:
        scenario = "cenario_3_tudo_pronto"

    log.info(
        "assistente_briefing_scenario",
        scenario=scenario,
        n_analyses=n_analyses,
        contatos_total=contatos_total,
        leads_quentes=totais["leads_quentes"],
        leads_mornos_gap=totais["leads_mornos_com_gap"],
        force_mini=force_mini,
    )

    # ── Step 5: Format messages per scenario ──────────────────────────────────
    msgs_rich: list[str] = []
    dia_fraco: bool

    if scenario == "cenario_1_tudo_zerado":
        msg1 = (
            f"📋 *Acme Imóveis — {context['data']}*\n\n"
            "💤 Sem atividade hoje. Sistema rodou normalmente."
        )
        dia_fraco = True

    elif scenario == "cenario_2_whatsapp_sem_inspector":
        msg1 = _format_msg1_metrics(context, dia_fraco=False) + _FOOTER_INSPECTOR_PENDENTE
        dia_fraco = True

    elif scenario == "edge_inspector_sem_whatsapp":
        dia_fraco = _is_dia_fraco(context) or force_mini
        _base = _format_briefing_mini(context) if dia_fraco else _format_msg1_metrics(context, dia_fraco=False)
        # Snapshot disponível com zeros = sem atividade WA ainda hoje (ex: cedo demais).
        # Fallback com zeros = Evolution genuinamente offline.
        _warning = _WARNING_SEM_ATIVIDADE_WHATSAPP if _usou_snapshot else _WARNING_EVOLUTION_OFFLINE
        msg1 = _base + _warning
        if not dia_fraco:
            msgs_rich, token_stats = _format_briefing_rich(context)

    else:  # cenario_3_tudo_pronto
        dia_fraco = _is_dia_fraco(context) or force_mini
        if dia_fraco:
            msg1 = _format_briefing_mini(context)
        else:
            msg1 = _format_msg1_metrics(context, dia_fraco=False)
            msgs_rich, token_stats = _format_briefing_rich(context)

    # ── Step 5b: conversas que a IA leu e acha que estão fechando ─────────────
    # Desde 22/10: o briefing da noite lê as conversas do dia com o Haiku 5.5.
    # Falha aqui nunca derruba o briefing: sai sem o bloco.
    if scenario != "cenario_1_tudo_zerado":
        try:
            _bloco_ia = _bloco_leitura_ia()
            if _bloco_ia:
                msgs_rich = list(msgs_rich) + [_bloco_ia]
        except Exception as exc:  # noqa: BLE001
            log.warning("assistente_leitura_ia_falhou", erro=str(exc)[:120])

    # ── Step 6: Send (if not dry_run) ─────────────────────────────────────────
    dona_status = "—"
    pessoa_status = "—"
    # send_failed = houve tentativa de envio que FALHOU (não conta "off"/"skip").
    # Propagado no retorno pra que o runner saia com exit != 0 (fix E1 → retry).
    send_failed = False
    if not dry_run:
        all_msgs = [msg1] + list(msgs_rich)

        # ── Dona via Telegram ──────────────────────────────────────────────
        # Idempotência POR MENSAGEM (P0): num re-run do mesmo dia, reenvia só as
        # mensagens que AINDA não saíram (evita dobro E não perde narrativa que
        # ficou pra trás num run que crashou no meio). Se todas já saíram, pula.
        with SessionLocal() as _sess:
            dona_sent_before = _already_sent_contents_today(_sess, "dona")
        dona_pending = [m for m in all_msgs if m not in dona_sent_before]
        if not dona_pending:
            dona_status = "skip (já entregue hoje)"
            log.warning("assistente_dona_skip_ja_entregue", scenario=scenario)
            with SessionLocal() as session:
                _mark_consumed_dona(session, analysis_ids)
        else:
            if len(dona_pending) < len(all_msgs):
                log.info(
                    "assistente_dona_reenvio_parcial",
                    scenario=scenario,
                    total=len(all_msgs),
                    pendentes=len(dona_pending),
                )
            try:
                telegram = TelegramTool()
                results_dona = []
                for i, m in enumerate(dona_pending):
                    r = telegram.send_to_dona(m)
                    results_dona.append(r)
                    log.info("assistente_msg_sent_dona", idx=i + 1, result=r, scenario=scenario)
                dona_ok = all(str(r).startswith("ok") for r in results_dona)
                dona_status = "✓" if dona_ok else "✗"
                if dona_ok:
                    with SessionLocal() as session:
                        _mark_consumed_dona(session, analysis_ids)
                else:
                    send_failed = True
                    _notificar_falha_briefing("Dona", "envio Telegram retornou erro")
            except Exception as exc:
                log.exception("assistente_dona_send_failed", error=str(exc))
                dona_status = f"✗ ({exc.__class__.__name__})"
                send_failed = True
                _notificar_falha_briefing("Dona", exc.__class__.__name__)

        # ── Pessoa Pessoa9 via TELEGRAM (bot corretor) ────────────────────────────
        # Decisão da Dona 01/07/2026: 2955 nunca inicia conversa; a cópia do
        # resumo vai pro Telegram da Pessoa via bot corretor.
        from app.tools.telegram import send_to_pessoa_telegram, _corretor_config
        if _corretor_config() is None and not settings.dry_run:
            pessoa_status = "off (corretor.env indisponível)"
            log.info("assistente_pessoa_disabled")
        else:
            with SessionLocal() as _sess:
                pessoa_sent_before = _already_sent_contents_today(_sess, "pessoa")
            pessoa_pending = [m for m in all_msgs if m not in pessoa_sent_before]
            if not pessoa_pending:
                pessoa_status = "skip (já entregue hoje)"
                log.warning("assistente_pessoa_skip_ja_entregue", scenario=scenario)
                with SessionLocal() as session:
                    _mark_consumed_pessoa(session, analysis_ids)
            else:
                if len(pessoa_pending) < len(all_msgs):
                    log.info(
                        "assistente_pessoa_reenvio_parcial",
                        scenario=scenario,
                        total=len(all_msgs),
                        pendentes=len(pessoa_pending),
                    )
                try:
                    results_pessoa = []
                    for i, m in enumerate(pessoa_pending):
                        r = send_to_pessoa_telegram(m)
                        results_pessoa.append(r)
                        log.info("assistente_msg_sent_pessoa", idx=i + 1, ok=r, scenario=scenario)
                    pessoa_ok = all(results_pessoa)
                    pessoa_status = "✓" if pessoa_ok else "✗"
                    if pessoa_ok:
                        with SessionLocal() as session:
                            _mark_consumed_pessoa(session, analysis_ids)
                    else:
                        send_failed = True
                        _notificar_falha_briefing("Pessoa", "envio Telegram (corretor) retornou False")
                except Exception as exc:
                    log.exception("assistente_pessoa_send_failed", error=str(exc))
                    pessoa_status = f"✗ ({exc.__class__.__name__})"
                    send_failed = True
                    _notificar_falha_briefing("Pessoa", exc.__class__.__name__)

        log.info(
            "assistente_send_summary",
            dona=dona_status,
            pessoa=pessoa_status,
            scenario=scenario,
            n_msgs=len(all_msgs),
            send_failed=send_failed,
        )
    else:
        log.info(
            "assistente_briefing_dry_run",
            scenario=scenario,
            n_analyses=n_analyses,
            contatos_total=contatos_total,
            dia_fraco=dia_fraco,
            n_msgs_rich=len(msgs_rich),
            tokens=token_stats,
        )

    elapsed = time.monotonic() - start
    log.info(
        "assistente_briefing_complete",
        scenario=scenario,
        leads_processados=n_analyses,
        tokens_input=token_stats["input"],
        tokens_output=token_stats["output"],
        elapsed_s=round(elapsed, 1),
        dry_run=dry_run,
    )
    return {
        "briefing": msg1,                                           # backward compat
        "briefing_msg1": msg1,
        "briefing_msg2": msgs_rich[0] if msgs_rich else None,      # backward compat
        "briefing_msgs_rich": msgs_rich,
        "scenario": scenario,
        "leads_processados": n_analyses,
        "tokens": token_stats,
        "elapsed_s": round(elapsed, 1),
        "dry_run": dry_run,
        "dia_fraco": dia_fraco,
        "dona_status": dona_status,
        "pessoa_status": pessoa_status,
        "send_failed": send_failed,
    }


# ── Agno Agent (registro no AgentOS) ─────────────────────────────────────────

def build_agente_assistente(db: PostgresDb) -> Agent:
    """
    Retorna Agno Agent para registro no AgentOS.

    Para invocação manual via control plane.
    Execução agendada: usar run_daily_briefing() diretamente.
    """
    return Agent(
        name="agente-assistente",
        id="agente-assistente",
        model=Claude(id=settings.model_analyzer),
        db=db,
        tools=[TelegramTool()],
        description=SYSTEM_PROMPT_DONA,
        instructions=[
            "Quando invocado manualmente, execute o briefing diário da Acme Imóveis.",
            "Use a TelegramTool para enviar o briefing para a Dona.",
            "Leia os lead_analyses do dia e formate o resumo gerencial.",
        ],
        markdown=True,
    )
