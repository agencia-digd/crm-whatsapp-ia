"""
Briefing matinal pra corretora — coleta dados, monta texto, envia via Evolution.

Determinístico (sem LLM). Privacidade: TODA query filtra por corretora_id.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select, text as sql_text

from app.config import settings
from app.db.models import Corretora, LeadAnalysis
from app.db.session import SessionLocal
from app.integrations.evolution_client import EvolutionClient
from app.integrations.gateway_client import GatewayClient
from app.services.conversations import append_message

log = structlog.get_logger()

SP_TZ = ZoneInfo("America/Sao_Paulo")

# ── Mapping nome corretora ↔ prefixo no Calendar ↔ label na aba Remarketing ──
CORRETORA_INFO = {
    "Pessoa3":   {"prefixo_calendar": "Pessoa",      "label_remarketing": "Pessoa"},
    "Pessoa5":    {"prefixo_calendar": "Pessoa10",       "label_remarketing": "Pessoa10"},
    "Pessoa1": {"prefixo_calendar": "Pessoa11",       "label_remarketing": "Pessoa2"},
}

DIAS_PT = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]

MONTH_PT = [
    "", "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
]


# ── Datatypes ────────────────────────────────────────────────────────────────


@dataclass
class VisitaSemanal:
    dia_semana: str
    data: date
    hora: str
    lead_nome: str
    bairro: str | None
    status: Literal["futura", "compareceu", "no_show", "sem_status"]
    cor_emoji: str


@dataclass
class LeadRetomar:
    nome: str
    contexto: str
    mensagem_sugerida: str | None


@dataclass
class CoachingItem:
    lead_nome: str
    trecho_corretora: str | None
    observacao: str
    sugestao: str | None
    categoria: str
    origem: str


@dataclass
class ConversasJanela:
    enviadas: int
    recebidas: int
    contatos_unicos: int
    destinatarios: int
    responderam: int
    pct_respondeu: int


@dataclass
class LeadQuente:
    nome: str
    razao: str


@dataclass
class DadosBriefing:
    corretora_nome: str
    ligacoes_semana: int
    contatos_semana: int
    tarefas_atrasadas: int | None
    visitas_semana: list[VisitaSemanal] = field(default_factory=list)
    leads_retomar: list[LeadRetomar] = field(default_factory=list)
    coaching: list[CoachingItem] = field(default_factory=list)
    snapshot_disponivel: bool = True
    conversas_24h: ConversasJanela | None = None
    leads_quentes: list[LeadQuente] = field(default_factory=list)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _now_sp() -> datetime:
    return datetime.now(SP_TZ)


def _week_bounds_sp(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Segunda 00:00 SP até domingo 23:59:59 SP da semana corrente."""
    n = (now or _now_sp()).astimezone(SP_TZ)
    monday = n - timedelta(days=n.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    sunday_end = monday + timedelta(days=7) - timedelta(seconds=1)
    return monday, sunday_end


def _week_of_month(day: int) -> int:
    if day <= 7: return 1
    if day <= 14: return 2
    if day <= 21: return 3
    return 4


def _color_to_status(color_id: str | None, start_dt_sp: datetime,
                     hoje_sp: date) -> tuple[str, str]:
    """Retorna (status, emoji)."""
    start_date = start_dt_sp.date()
    if start_date >= hoje_sp:
        return ("futura", "🟠")
    cid = str(color_id or "")
    if cid == "10":
        return ("compareceu", "🟢")
    if cid == "11":
        return ("no_show", "🔴")
    return ("sem_status", "🟡")


# ── Coletores (cada um faz UMA query filtrada por corretora_id) ──────────────


def _coletar_ligacoes_semana_snapshot(corretora_id: int) -> tuple[int, int]:
    """
    Lê (ligacoes, atendeu) da view vw_semana_atual_corretora.

    Retorna (0, 0) se a view não tem linha (semana ainda sem registro).
    """
    with SessionLocal() as s:
        row = s.execute(
            sql_text(
                "SELECT COALESCE(ligacoes, 0), COALESCE(ligacoes_atendeu, 0) "
                "FROM vw_semana_atual_corretora WHERE corretora_id = :cid"
            ),
            {"cid": corretora_id},
        ).first()
    if not row:
        return (0, 0)
    return (int(row[0] or 0), int(row[1] or 0))


def _coletar_metricas_dia_snapshot(corretora_id: int) -> dict[str, Any] | None:
    """
    Lê linha de daily_metrics_corretora pra HOJE.

    Retorna None se o Inspector ainda não consolidou hoje (briefing
    deve mostrar 'indisponível' nos campos vindos daqui).
    """
    with SessionLocal() as s:
        row = s.execute(
            sql_text(
                "SELECT total_leads_mes, atendidos_mes, tarefas_atrasadas, "
                "fontes_falhas "
                "FROM daily_metrics_corretora "
                "WHERE corretora_id = :cid AND data = CURRENT_DATE"
            ),
            {"cid": corretora_id},
        ).first()
    if not row:
        return None
    return {
        "total_leads_mes": row[0],
        "atendidos_mes": row[1],
        "tarefas_atrasadas": row[2],
        "fontes_falhas": list(row[3] or []),
    }


# Mantida só pra compatibilidade do teste antigo de privacidade (label sheets).
def _coletar_ligacoes_semana(corretora_label_remarketing: str,
                             now: datetime) -> tuple[int, int]:
    """
    Lê aba Remarketing da planilha Acme. Retorna (ligacoes, atendeu).

    'Contatos' aqui = atendeu (pessoas que de fato falaram).
    """
    try:
        # Import diferido pra não bater no Sheets em testes
        from gspread.exceptions import APIError  # noqa: F401
        from google.oauth2.service_account import Credentials
        import gspread

        cred_path = settings.acme_sheets_cred or ""
        if not cred_path or not Path(cred_path).exists():
            log.warning("briefing_no_sheets_cred")
            return (0, 0)

        creds = Credentials.from_service_account_file(
            cred_path,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive.readonly",
            ],
        )
        sh = gspread.authorize(creds).open_by_key(
            "SPREADSHEET_ID"
        )
        ws = sh.worksheet("Remarketing")
        rows = ws.get_values("A1:Z100")

        month_name = MONTH_PT[now.month]
        month_col = None
        for ci, cell in enumerate(rows[0] if rows else []):
            if (cell or "").strip().lower() == month_name.lower():
                month_col = ci
                break
        if month_col is None:
            log.info("briefing_no_month_col", month=month_name)
            return (0, 0)

        week = _week_of_month(now.day)
        week_col = month_col + (week - 1)

        target = corretora_label_remarketing.lower()
        corr_row = None
        for ri, row in enumerate(rows):
            if row and (row[0] or "").strip().lower() == target:
                corr_row = ri
                break
        if corr_row is None:
            return (0, 0)

        def _get(ri: int, ci: int) -> int:
            if ri < len(rows) and ci < len(rows[ri]):
                v = (rows[ri][ci] or "").strip().replace(".", "")
                try:
                    return int(v)
                except ValueError:
                    return 0
            return 0

        ligacoes = _get(corr_row + 1, week_col)
        atendeu  = _get(corr_row + 2, week_col)
        return (ligacoes, atendeu)
    except Exception as e:
        log.error("briefing_sheets_error", error=str(e))
        return (0, 0)


def _coletar_tarefas_atrasadas(corretora_id: int | None) -> int:
    """Tarefas atrasadas da corretora — do PAINEL (`tarefas_corretora`).

    Fonte trocada em 31/07/2026 (Dona: "a gente oficialmente não usa mais
    Kommo"). Até aqui vinha do Kommo, e a divergência era grande:

        corretora    Kommo dizia    painel real
        Pessoa5           89             37
        Pessoa3          52             11
        Pessoa1        47             44

    A corretora abria o painel, via 11, e o briefing dizia 52. Número errado
    queima a confiança no briefing inteiro — se esse está furado, por que
    acreditar nos outros?

    Mesma definição que o painel usa: aberta e com data anterior a hoje (SP).
    """
    if not corretora_id:
        return 0
    try:
        with SessionLocal() as s:
            n = s.execute(
                sql_text(
                    """
                    SELECT count(*) FROM tarefas_corretora
                     WHERE corretora_id = :cid
                       AND status = 'aberta'
                       AND data_tarefa < (now() AT TIME ZONE 'America/Sao_Paulo')::date
                    """
                ),
                {"cid": int(corretora_id)},
            ).scalar()
        return int(n or 0)
    except Exception as e:  # noqa: BLE001
        log.error("briefing_tarefas_atrasadas_erro", error=str(e)[:160])
        return 0


def _coletar_conversas_24h(corretora_id: int) -> ConversasJanela:
    """
    Métricas de conversas das últimas 24h, da instância Evolution da
    corretora. Filtra por corretora_id (privacidade).
    """
    with SessionLocal() as s:
        row = s.execute(
            sql_text(
                """
                WITH base AS (
                  SELECT contato_telefone, from_corretora
                  FROM mensagens_whatsapp
                  WHERE corretora_id = :cid
                    AND timestamp_evolution >= NOW() - INTERVAL '24 hours'
                ),
                enviados AS (
                  SELECT DISTINCT contato_telefone
                  FROM base WHERE from_corretora = true
                ),
                respondidos AS (
                  SELECT DISTINCT contato_telefone
                  FROM base
                  WHERE from_corretora = false
                    AND contato_telefone IN (SELECT contato_telefone FROM enviados)
                )
                SELECT
                  (SELECT COUNT(*) FROM base WHERE from_corretora) AS env,
                  (SELECT COUNT(*) FROM base WHERE NOT from_corretora) AS rec,
                  (SELECT COUNT(DISTINCT contato_telefone) FROM base) AS uniq,
                  (SELECT COUNT(*) FROM enviados) AS dest,
                  (SELECT COUNT(*) FROM respondidos) AS resp
                """
            ),
            {"cid": corretora_id},
        ).first()
    env, rec, uniq, dest, resp = (int(row[i] or 0) for i in range(5))
    pct = int(round((resp / dest) * 100)) if dest else 0
    return ConversasJanela(
        enviadas=env, recebidas=rec, contatos_unicos=uniq,
        destinatarios=dest, responderam=resp, pct_respondeu=pct,
    )


def _coletar_leads_quentes(corretora_id: int, limit: int = 3) -> list[LeadQuente]:
    """
    Leads com temperatura='quente' nas últimas 36h (cobrindo o run noturno
    do Inspector). Razão extraída de conversa_resumo ou gap_identificado.
    """
    cutoff = (_now_sp() - timedelta(hours=36)).astimezone(timezone.utc).replace(tzinfo=None)
    with SessionLocal() as s:
        rows = s.execute(
            select(LeadAnalysis)
            .where(LeadAnalysis.corretora_id == corretora_id)
            .where(LeadAnalysis.temperatura == "quente")
            .where(LeadAnalysis.analyzed_at >= cutoff)
            .order_by(LeadAnalysis.analyzed_at.desc())
            .limit(limit * 2)
        ).scalars().all()
    seen: set[str] = set()
    out: list[LeadQuente] = []
    for r in rows:
        raw = r.raw_analysis or {}
        nome = (raw.get("lead_name") or "").strip() or "Lead"
        if nome in seen:
            continue
        seen.add(nome)
        razao = (raw.get("conversa_resumo")
                 or raw.get("gap_identificado")
                 or "").strip()
        # razão curta — primeira frase ou primeiros 220 chars
        if len(razao) > 220:
            cut = razao[:220].rsplit(" ", 1)[0]
            razao = cut + "…"
        out.append(LeadQuente(nome=nome, razao=razao or "Sem detalhes adicionais."))
        if len(out) >= limit:
            break
    return out


def _coletar_visitas_semana_pg(corretora_id: int,
                               week_start: datetime,
                               week_end: datetime) -> list[VisitaSemanal]:
    """
    Visitas da semana lidas da tabela visitas no Postgres (não do Gateway).
    Filtra por corretora_id.
    """
    hoje_sp = _now_sp().date()
    out: list[VisitaSemanal] = []
    with SessionLocal() as s:
        rows = s.execute(
            sql_text(
                """
                SELECT data_hora, lead_nome, bairro, status
                FROM visitas
                WHERE corretora_id = :cid
                  AND data_hora >= :ws
                  AND data_hora <= :we
                ORDER BY data_hora
                """
            ),
            {"cid": corretora_id, "ws": week_start, "we": week_end},
        ).all()
    for r in rows:
        dt_utc = r[0]
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        dt_sp = dt_utc.astimezone(SP_TZ)
        status_pg = r[3] or "agendada"
        # mapping status postgres → status briefing + emoji
        if status_pg == "compareceu":
            status, emoji = "compareceu", "🟢"
        elif status_pg == "no_show":
            status, emoji = "no_show", "🔴"
        elif status_pg == "cancelada":
            status, emoji = "sem_status", "⚪️"
        elif status_pg == "pendente_confirmacao":
            status, emoji = "sem_status", "🟡"
        else:  # agendada
            status, emoji = "futura", "🟠"
        out.append(VisitaSemanal(
            dia_semana=DIAS_PT[dt_sp.weekday()],
            data=dt_sp.date(),
            hora=dt_sp.strftime("%Hh%M") if dt_sp.minute else dt_sp.strftime("%Hh"),
            lead_nome=r[1] or "Lead",
            bairro=r[2],
            status=status,
            cor_emoji=emoji,
        ))
    return out


def _coletar_visitas_semana(prefixo_calendar: str,
                            week_start: datetime,
                            week_end: datetime) -> list[VisitaSemanal]:
    try:
        client = GatewayClient()
        events = client.list_events(time_min=week_start, time_max=week_end,
                                    max_results=250)
    except Exception as e:
        log.error("briefing_gateway_error", error=str(e))
        return []

    hoje_sp = _now_sp().date()
    out: list[VisitaSemanal] = []
    for e in events:
        summary = e.get("summary", "") or ""
        # Match prefixo entre [ ] ou ( ) (a confirmar)
        prefixo_match = None
        for opener, closer in [("[", "]"), ("(", ")")]:
            if summary.startswith(opener):
                end = summary.find(closer)
                if end > 0:
                    prefixo_match = summary[1:end].strip()
                    break
        if prefixo_match != prefixo_calendar:
            continue

        start = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date")
        if not start:
            continue
        try:
            if "T" in start:
                start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            else:
                start_dt = datetime.fromisoformat(start + "T00:00:00+00:00")
        except ValueError:
            continue
        start_sp = start_dt.astimezone(SP_TZ)

        # Lead nome: tira "[Prefixo] " do summary
        lead_nome = summary
        for opener, closer in [("[", "]"), ("(", ")")]:
            idx = summary.find(closer)
            if summary.startswith(opener) and idx > 0:
                lead_nome = summary[idx + 1:].strip() or summary
                break

        bairro = None  # description não foi padronizado; deixa None por ora

        status, emoji = _color_to_status(e.get("colorId"), start_sp, hoje_sp)

        out.append(VisitaSemanal(
            dia_semana=DIAS_PT[start_sp.weekday()],
            data=start_sp.date(),
            hora=start_sp.strftime("%Hh%M") if start_sp.minute else start_sp.strftime("%Hh"),
            lead_nome=lead_nome,
            bairro=bairro,
            status=status,
            cor_emoji=emoji,
        ))

    out.sort(key=lambda v: (v.data, v.hora))
    return out


def coletar_coaching(corretora_id: int, limite: int = 5) -> list[CoachingItem]:
    """
    Lê coaching_observations das últimas 24h dessa corretora.

    PRIVACIDADE: filtro por corretora_id obrigatório.
    """
    rows = []
    with SessionLocal() as s:
        result = s.execute(
            sql_text(
                "SELECT lead_nome, trecho_corretora, observacao, sugestao, "
                "       categoria, origem "
                "FROM coaching_observations "
                "WHERE corretora_id = :cid "
                "  AND analyzed_at >= NOW() - INTERVAL '24 hours' "
                "ORDER BY analyzed_at DESC "
                "LIMIT :lim"
            ),
            {"cid": corretora_id, "lim": limite},
        ).all()
        rows = list(result)
    return [
        CoachingItem(
            lead_nome=r[0] or "Lead",
            trecho_corretora=r[1],
            observacao=r[2] or "",
            sugestao=r[3],
            categoria=r[4] or "outro",
            origem=r[5] or "lead_quente",
        )
        for r in rows
    ]


def _coletar_leads_retomar(corretora_id: int, limit: int = 5) -> list[LeadRetomar]:
    """
    Top N leads quentes/mornos com gap detectado, do dia mais recente,
    pra retomar. Filtra por corretora_id (regra de ouro).
    """
    cutoff = (_now_sp() - timedelta(hours=36)).astimezone(timezone.utc)
    with SessionLocal() as s:
        rows = s.execute(
            select(LeadAnalysis)
            .where(LeadAnalysis.corretora_id == corretora_id)
            .where(LeadAnalysis.analyzed_at >= cutoff.replace(tzinfo=None))
            .where(LeadAnalysis.has_gap.is_(True))
            .where(LeadAnalysis.temperatura.in_(["quente", "morno"]))
            .order_by(LeadAnalysis.analyzed_at.desc())
            .limit(limit * 2)
        ).scalars().all()

    seen: set[str] = set()
    out: list[LeadRetomar] = []
    for r in rows:
        if len(out) >= limit:
            break
        raw = r.raw_analysis or {}
        nome = raw.get("lead_name") or "Lead"
        if nome in seen:
            continue
        seen.add(nome)
        contexto = (raw.get("conversa_resumo") or raw.get("gap_identificado") or "").strip()
        msg = raw.get("abordagem_sugerida")
        out.append(LeadRetomar(nome=nome, contexto=contexto or "Sem contexto adicional.",
                               mensagem_sugerida=msg))
    return out


# ── Coletor mestre ───────────────────────────────────────────────────────────


def coletar_dados(corretora_id: int) -> DadosBriefing:
    """PRIVACIDADE: tudo passa pelo corretora_id. Falha se não achar."""
    with SessionLocal() as s:
        corretora = s.execute(
            select(Corretora)
            .where(Corretora.id == corretora_id)
            .where(Corretora.ativo.is_(True))
        ).scalar_one_or_none()
    if not corretora:
        raise ValueError(f"corretora_id {corretora_id} não encontrada/ativa")

    info = CORRETORA_INFO.get(corretora.nome)
    if not info:
        raise ValueError(f"corretora '{corretora.nome}' sem mapeamento de prefixo")

    now = _now_sp()
    week_start, week_end = _week_bounds_sp(now)

    ligacoes, atendeu = _coletar_ligacoes_semana_snapshot(corretora.id)
    metricas = _coletar_metricas_dia_snapshot(corretora.id)
    snapshot_ok = metricas is not None

    # Tarefas atrasadas: fonte PAINEL (31/07 — o Kommo saiu de operação).
    tarefas = _coletar_tarefas_atrasadas(corretora.id)

    visitas = _coletar_visitas_semana_pg(corretora.id, week_start, week_end)
    coaching_items = coletar_coaching(corretora.id, limite=2)
    conversas = _coletar_conversas_24h(corretora.id)
    quentes = _coletar_leads_quentes(corretora.id, limit=3)

    return DadosBriefing(
        corretora_nome=corretora.nome,
        ligacoes_semana=ligacoes,
        contatos_semana=atendeu,
        tarefas_atrasadas=tarefas,
        visitas_semana=visitas,
        leads_retomar=[],
        coaching=coaching_items,
        snapshot_disponivel=snapshot_ok,
        conversas_24h=conversas,
        leads_quentes=quentes,
    )


# ── Montagem do texto ────────────────────────────────────────────────────────
# Extraída pra app/delivery/corretora_formatter.py (Onda 2). Re-exportada aqui
# pra preservar os imports públicos (tests, enviar_briefing) — o serviço segue
# sendo o entry-point (coleta dados + envia).
from app.delivery.corretora_formatter import (  # noqa: E402
    _build_cta_atendimento,
    _label_status,
    montar_texto,
)


# ── Envio ────────────────────────────────────────────────────────────────────


def enviar_briefing(
    corretora_id: int,
    destino_phone: str,
    registrar_em_conversations: bool = True,
) -> dict[str, Any]:
    """
    Coleta dados, monta texto, envia via Evolution (instância assistente).
    Retorna {status, sent_at, message_length, preview, error?}.
    """
    try:
        dados = coletar_dados(corretora_id)
    except ValueError as e:
        return {"status": "error", "error": str(e), "preview": None}

    texto = montar_texto(dados)

    try:
        client = EvolutionClient()
        client.send_text(
            instance_name=settings.evolution_assistente_instance,
            to_number=destino_phone,
            text=texto,
        )
    except Exception as e:
        log.error("briefing_send_failed", corretora_id=corretora_id,
                  to_last4=destino_phone[-4:], error=str(e))
        return {
            "status": "error",
            "error": f"send_text falhou: {e}",
            "preview": texto,
            "message_length": len(texto),
        }

    sent_at = _now_sp().isoformat()
    if registrar_em_conversations:
        try:
            append_message(
                phone=destino_phone,
                sender_kind="assistant",
                sender_id=None,
                message_role="assistant",
                text=texto,
            )
        except Exception as e:
            log.error("briefing_persist_failed", error=str(e))

    return {
        "status": "sent",
        "sent_at": sent_at,
        "message_length": len(texto),
        "preview": texto,
    }
