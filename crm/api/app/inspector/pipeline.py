"""
Lead Inspector — pipeline determinístico de análise diária.

NÃO é um Agente Agno. É um pipeline Python que orquestra:
  1. Busca de leads no Kommo (janela: criados últimos 2 dias OU atualizados hoje)
  2. Busca de conversa no Postgres/mensagens_whatsapp (janela: 72h)
  3. Filtro de janela de conexão (só msgs após evolution_connected_since)
  4. Classificação por modo:
     - conversa_whatsapp: tem msgs pós-conexão → Classifier (CLI) + Analyzer (CLI)
     - tarefa_atrasada:   sem msgs, mas tarefa Kommo vencida → determinístico, zero LLM
     - sem_atividade:     nem msgs nem tarefas → frio sem ação
     - sem_dado_historico: conexão Evolution posterior à atividade do lead
  5. Upsert em leads_snapshot + INSERT em lead_analyses

Janela de conexão Evolution:
  Mensagens anteriores a corretora.evolution_connected_since são descartadas.
  Se após o filtro restam 0 mensagens mas havia mensagens: sem_dado_historico.
  Esta limitação é documentada em CLAUDE.md § Limitações conhecidas.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import structlog
from pydantic import BaseModel
from sqlalchemy import case, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import Corretora, InspectorRun, LeadAnalysis, LeadSnapshot
from app.db.session import SessionLocal, engine
from app.inspector.analyzer import DeepAnalysisResult, analyze_lead, dias_sem_msg_lead
from app.inspector.classifier import ClassificationResult, classify_lead
from app.inspector.coaching import analisar_coaching, gravar_observacao
from app.inspector.conversation_reader import get_conversation_db, _normalize_phone
from app.inspector.fonte_leads import get_leads_for_inspector_db
from app.tools.evolution import EvolutionTool

log = structlog.get_logger()

AnalysisMode = Literal[
    "conversa_whatsapp",
    "tarefa_atrasada",
    "sem_atividade",
    "sem_atividade_hoje",
    "instancia_offline",
    "sem_dado_historico",
]


# ── Output schema ─────────────────────────────────────────────────────────────

class LeadAnalysisOutput(BaseModel):
    lead_id: str
    lead_name: str
    corretora: str
    corretora_id: int
    analysis_mode: AnalysisMode
    status: str
    temperatura: str
    ultima_msg_at: str | None
    ultima_msg_de: str | None
    conversa_resumo: str | None
    o_que_corretora_disse: str | None
    gap_identificado: str | None
    abordagem_sugerida: str | None
    tarefas_kommo_atrasadas: list[str]
    tarefas_atrasadas_detail: list[dict[str, Any]] | None = None
    etapa_kommo_desatualizada: bool
    skipped_reason: str | None = None
    classifier_reason: str | None = None
    tokens_classifier: int = 0
    tokens_analyzer: int = 0
    entrou_por: str | None = None
    # Auditoria dos guardrails (I4/I5/I6)
    dias_sem_msg_lead: int | None = None
    temperatura_capped_staleness: bool = False
    citacao_verificada: bool = True
    respiro_valido: bool = True
    analise_suspeita: bool = False
    # O classifier teria vetado a análise profunda deste lead? (26/08/2026)
    # Vira registro em vez de porta — é assim que se mede, depois, se o veto
    # do Haiku estava certo: basta ver quais destes viraram visita ou venda.
    classifier_teria_descartado: bool = False


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _unix_to_dt(ts: Any) -> datetime | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def _upsert_snapshot(lead: dict[str, Any], corretora_id: int) -> None:
    with SessionLocal() as session:
        stmt = (
            pg_insert(LeadSnapshot)
            .values(
                lead_id=lead["lead_id"],
                nome=lead.get("nome") or "",
                telefone=lead.get("telefone") or "",
                corretora_id=corretora_id,
                etapa_kommo=str(lead.get("etapa_id") or ""),
                origem=lead.get("origem") or "",
                criado_em_kommo=_unix_to_dt(lead.get("criado_em")),
                ultima_sync=datetime.now(timezone.utc),
            )
            .on_conflict_do_update(
                index_elements=["lead_id"],
                set_={
                    # Proteção do sync (migration 0049): se o nome foi editado à
                    # mão no painel (nome_editado_manual=true), NÃO sobrescreve —
                    # mantém o valor atual da linha. O resto do sync segue igual.
                    "nome": case(
                        (
                            LeadSnapshot.nome_editado_manual.is_(True),
                            LeadSnapshot.nome,
                        ),
                        else_=(lead.get("nome") or ""),
                    ),
                    # Proteção do sync (migration 0050): telefone corrigido à mão
                    # no painel (telefone_editado_manual=true) NÃO é sobrescrito —
                    # mantém o valor atual da linha.
                    "telefone": case(
                        (
                            LeadSnapshot.telefone_editado_manual.is_(True),
                            LeadSnapshot.telefone,
                        ),
                        else_=(lead.get("telefone") or ""),
                    ),
                    # Proteção do sync (migration 0061): lead TRANSFERIDO de
                    # corretora no painel (corretora_editado_manual=true) NÃO é
                    # devolvido pra corretora do Kommo — mantém a dona atual.
                    "corretora_id": case(
                        (
                            LeadSnapshot.corretora_editado_manual.is_(True),
                            LeadSnapshot.corretora_id,
                        ),
                        else_=corretora_id,
                    ),
                    "etapa_kommo": str(lead.get("etapa_id") or ""),
                    "origem": lead.get("origem") or "",
                    "ultima_sync": datetime.now(timezone.utc),
                },
            )
        )
        session.execute(stmt)
        session.commit()


def _dono_canonico(session: Any, lead_id: Any) -> int | None:
    """Dono canônico do lead: `leads.owner_id` da espinha, casando pelo
    external_ref (o `lead_id` do Inspector é o id do lead no Kommo).

    Existe pra estancar o drift de dono na origem (L3 item 4): gravar
    `output.corretora_id` cru registra a corretora que o Inspector estava
    processando naquela passada, que NÃO é necessariamente a dona do lead —
    depois de uma transferência manual no painel (owner_editado_manual), o
    Kommo ainda devolve a dona antiga e a análise nascia apontando pra ela.
    Sem lead na espinha → None (o caller cai no comportamento antigo).
    """
    from app.db.models import Lead

    if lead_id is None:
        return None
    owner = (
        session.query(Lead.owner_id)
        .filter(Lead.external_ref == str(lead_id))
        .order_by(Lead.id.asc())
        .first()
    )
    return owner[0] if owner else None


def _save_analysis(output: LeadAnalysisOutput) -> None:
    with SessionLocal() as session:
        corretora_id = _dono_canonico(session, output.lead_id)
        if corretora_id is None:
            # Lead ainda não está na espinha (shadow não passou por ele) — cai
            # no id do Inspector, comportamento antigo.
            corretora_id = output.corretora_id
        elif corretora_id != output.corretora_id:
            log.info(
                "inspector_analise_dono_canonico",
                lead_id=output.lead_id,
                corretora_inspector=output.corretora_id,
                corretora_espinha=corretora_id,
            )
        record = LeadAnalysis(
            lead_id=output.lead_id,
            analyzed_at=datetime.now(timezone.utc),
            raw_analysis=output.model_dump(),
            status=output.status,
            temperatura=output.temperatura,
            has_gap=bool(output.gap_identificado),
            consumed_by_dona=False,
            consumed_by_corretora=False,
            corretora_id=corretora_id,
        )
        session.add(record)
        session.commit()


# ── Advisory lock (anti-concorrência por corretora) ──────────────────────────
#
# O Inspector pode ser disparado concorrente pra mesma corretora (timer +
# invocação manual, dois crons, retry sobreposto). Sem trava, isso DUPLICA as
# chamadas LLM (Classifier/Analyzer) — custo real. Usamos advisory lock de
# sessão do Postgres: `pg_try_advisory_lock(key1, key2)` é não-bloqueante e
# retorna False se outra sessão já segura a trava. Liberamos no finally.
#
# key1 é um namespace fixo (evita colisão com advisory locks de outros
# subsistemas que porventura usem a forma de dois inteiros); key2 é o
# corretora_id. Ambos int32 — corretora_id cabe folgado.
_ADVISORY_LOCK_NAMESPACE = 0x42455454  # "BETT" — namespace do Inspector Acme


def _try_acquire_corretora_lock(corretora_id: int) -> Any | None:
    """
    Tenta adquirir advisory lock de sessão pra corretora (não-bloqueante).

    Retorna a conexão que SEGURA a trava (mantida aberta até o unlock) se
    conseguiu, ou None se outra sessão já está processando a corretora.

    A trava é de SESSÃO (não de transação): fica presa à conexão retornada.
    O caller DEVE chamar _release_corretora_lock(conn, corretora_id) no finally
    pra liberar e devolver a conexão ao pool.
    """
    conn = engine.connect()
    try:
        got = conn.execute(
            text("SELECT pg_try_advisory_lock(:ns, :cid)"),
            {"ns": _ADVISORY_LOCK_NAMESPACE, "cid": int(corretora_id)},
        ).scalar()
    except Exception:
        conn.close()
        raise
    if got:
        return conn
    conn.close()
    return None


def _release_corretora_lock(conn: Any, corretora_id: int) -> None:
    """Libera o advisory lock e fecha a conexão. Best-effort (não levanta)."""
    if conn is None:
        return
    try:
        conn.execute(
            text("SELECT pg_advisory_unlock(:ns, :cid)"),
            {"ns": _ADVISORY_LOCK_NAMESPACE, "cid": int(corretora_id)},
        )
    except Exception:
        log.exception("advisory_unlock_failed", corretora_id=corretora_id)
    finally:
        try:
            conn.close()
        except Exception:
            log.exception("advisory_conn_close_failed", corretora_id=corretora_id)


# ── Inspector run management ──────────────────────────────────────────────────

_ORPHAN_THRESHOLD_HOURS = 2


def _mark_orphan_runs(corretora_id: int) -> None:
    """Marca como 'failed' runs 'running' com started_at > 2h (crash anterior)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_ORPHAN_THRESHOLD_HOURS)
    with SessionLocal() as s:
        s.query(InspectorRun).filter(
            InspectorRun.corretora_id == corretora_id,
            InspectorRun.status == "running",
            InspectorRun.started_at < cutoff,
        ).update(
            {"status": "failed", "erro": "orphan_run_detected", "finished_at": datetime.now(timezone.utc)},
            synchronize_session=False,
        )
        s.commit()


def _get_last_run_finished_at(corretora_id: int) -> datetime | None:
    """Retorna finished_at da última run 'completed' da corretora."""
    with SessionLocal() as s:
        row = (
            s.query(InspectorRun.finished_at)
            .filter(
                InspectorRun.corretora_id == corretora_id,
                InspectorRun.status == "completed",
            )
            .order_by(InspectorRun.finished_at.desc())
            .first()
        )
    return row[0] if row else None


def _compute_window_start(
    last_run_finished_at: datetime | None,
    evolution_connected_since: datetime | None,
) -> datetime:
    """
    Calcula início da janela de mensagens para esta execução.

    Regras:
    - Base: última run completed.finished_at (ou evolution_connected_since se 1ª execução)
    - Se não tem nem run anterior nem connected_since: fallback 72h atrás
    - Sempre >= evolution_connected_since (nunca pede mensagens antes da conexão)
    """
    now = datetime.now(timezone.utc)
    connected = (
        evolution_connected_since.replace(tzinfo=timezone.utc)
        if evolution_connected_since and evolution_connected_since.tzinfo is None
        else evolution_connected_since
    )

    if last_run_finished_at is not None:
        base = last_run_finished_at
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
    elif connected is not None:
        base = connected
    else:
        base = now - timedelta(hours=72)

    if connected and base < connected:
        base = connected

    return base


def _create_inspector_run(corretora_id: int, window_start: datetime) -> uuid.UUID:
    """Cria nova run com status='running'. Retorna o UUID da run."""
    run = InspectorRun(
        corretora_id=corretora_id,
        started_at=datetime.now(timezone.utc),
        status="running",
        window_start=window_start,
    )
    with SessionLocal() as s:
        s.add(run)
        s.commit()
        s.refresh(run)
        return run.id


def _complete_inspector_run(
    run_id: uuid.UUID,
    mensagens_processadas: int,
    leads_analisados: int,
) -> None:
    with SessionLocal() as s:
        s.query(InspectorRun).filter(InspectorRun.id == run_id).update(
            {
                "status": "completed",
                "finished_at": datetime.now(timezone.utc),
                "mensagens_processadas": mensagens_processadas,
                "leads_analisados": leads_analisados,
            },
            synchronize_session=False,
        )
        s.commit()


def _fail_inspector_run(run_id: uuid.UUID, erro: str) -> None:
    with SessionLocal() as s:
        s.query(InspectorRun).filter(InspectorRun.id == run_id).update(
            {
                "status": "failed",
                "finished_at": datetime.now(timezone.utc),
                "erro": erro[:500],
            },
            synchronize_session=False,
        )
        s.commit()


# ── Internos filter ───────────────────────────────────────────────────────────

def _load_internos_set() -> set[str]:
    """
    Carrega telefones normalizados de internos_acme (ativa=true).
    Retorna set de strings para lookup O(1) por lead.
    """
    with SessionLocal() as s:
        rows = s.execute(
            text("SELECT telefone_normalizado FROM internos_acme WHERE ativa = true")
        ).fetchall()
    return {r[0] for r in rows}


# ── Helpers ────────────────────────────────────────────────────────────────────

# Teto de janela do canal Cloud (Pessoa12): mesmo default do ler_thread_cloud (720h ≈
# 30 dias). _apply_connection_window depois corta o pré-conexão, como no Evolution.
_CLOUD_HOURS_BACK = 720


def _ler_conversa_cloud(
    lead_telefone: str,
    corretora_id: int | None = None,
) -> list[dict[str, Any]]:
    """Lê a conversa do canal 'cloud' da TABELA mensagens_whatsapp.

    Reusa ``ler_thread_cloud`` (cloud_corretora_a) — a MESMA fonte que o painel e a
    reanálise usam — e normaliza pro shape do get_conversation_db
    ({"from","text","at","type","transcricao","transcricao_status"}), que é o que
    o classifier/analyzer consomem. Import tardio evita ciclo (cloud_corretora_a
    importa peças de serviço que puxam o pipeline no boot).

    `corretora_id` virou informativo em 02/09: `ler_thread_cloud` resolve a
    thread por TELEFONE e não filtra mais por linha (fix do lead Rebeca — toque
    de remarketing sai pela Pessoa12 e sumia do chat da dona). Antes ele era
    obrigatório na prática, porque o filtro caía no default da Pessoa12 (3) e a
    chamada sem o argumento devolvia thread VAZIA pra Pessoa10, Pessoa11 e Corretor — o
    Inspector analisava lead sem conversa nenhuma, virando um relatório de
    tarefa vencida do Kommo. Segue passado pra manter a assinatura explícita.
    """
    from app.api.cloud_corretora_a import ler_thread_cloud

    thread = ler_thread_cloud(
        lead_telefone, hours_back=_CLOUD_HOURS_BACK, corretora_id=corretora_id
    )
    out: list[dict[str, Any]] = []
    for m in thread:
        out.append({
            "from": m.get("from"),
            "text": m.get("text") or "",
            "at": m.get("at") or "",
            "type": m.get("type") or "text",
            "transcricao": m.get("transcricao"),
            "transcricao_status": None,
        })
    return out


def _apply_connection_window(
    messages: list[dict[str, Any]],
    connected_since: datetime | None,
) -> list[dict[str, Any]]:
    """Descarta mensagens anteriores a evolution_connected_since."""
    if not connected_since:
        return messages
    # Só assume UTC quando o timestamp é naive; se já tem fuso (ex.: -03:00),
    # CONVERTE em vez de sobrescrever — replace() incondicional deslocava o marco
    # da janela em 3h e incluía/excluía mensagens erradas (Codex #10).
    since_aware = (
        connected_since.replace(tzinfo=timezone.utc)
        if connected_since.tzinfo is None
        else connected_since.astimezone(timezone.utc)
    )
    result = []
    for m in messages:
        at_str = m.get("at") or ""
        if not at_str:
            continue
        try:
            msg_dt = datetime.fromisoformat(at_str)
            if msg_dt.tzinfo is None:
                msg_dt = msg_dt.replace(tzinfo=timezone.utc)
            if msg_dt >= since_aware:
                result.append(m)
        except ValueError:
            pass
    return result


def _last_message_meta(messages: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """Retorna (ultima_msg_at, ultima_msg_de) da lista cronológica."""
    if not messages:
        return None, None
    last = messages[-1]
    return last.get("at"), last.get("from")


def _last_message_is_today_sp(messages: list[dict[str, Any]]) -> bool:
    """True se a última mensagem da conversa é de hoje no horário de São Paulo."""
    if not messages:
        return False
    from zoneinfo import ZoneInfo
    sp = ZoneInfo("America/Sao_Paulo")
    today_sp = datetime.now(sp).date()
    last_at = messages[-1].get("at") or ""
    if not last_at:
        return False
    try:
        msg_dt = datetime.fromisoformat(last_at)
        if msg_dt.tzinfo is None:
            msg_dt = msg_dt.replace(tzinfo=timezone.utc)
        return msg_dt.astimezone(sp).date() == today_sp
    except ValueError:
        return False


# ── Guardrails de staleness / coerência (I4 / I6) ──────────────────────────────

# Rebaixamento de temperatura por silêncio do lead (I4.2).
_STALENESS_CAP_MORNO_DIAS = 14   # > 14d: quente → morno
_STALENESS_CAP_FRIO_DIAS = 30    # > 30d: qualquer → frio

# Teto de análises profundas por rodada (26/08/2026).
#
# Não é orçamento: é freio de mão. O volume real é de 43 a 94 leads/dia, então
# 150 nunca encosta na operação normal. Existe para o dia em que uma importação
# em massa ou um bug de janela devolver 2.000 leads — em vez de rodar o Sonnet
# duas mil vezes e queimar a cota, o pipeline para no teto e registra.
#
# Leads acima do teto continuam no relatório com a classificação do Haiku; só
# não ganham a análise profunda.
MAX_DEEP_ANALISES_POR_RODADA = 150


def _cap_temperatura_staleness(temperatura: str, dias_silencio: int | None) -> tuple[str, bool]:
    """
    Rebaixa temperatura conforme silêncio do lead (I4.2). Retorna (nova_temp, capped?).

    > 30 dias → frio · > 14 dias → no máximo morno (quente vira morno).
    """
    if dias_silencio is None:
        return temperatura, False
    if dias_silencio > _STALENESS_CAP_FRIO_DIAS and temperatura != "frio":
        return "frio", True
    if dias_silencio > _STALENESS_CAP_MORNO_DIAS and temperatura == "quente":
        return "morno", True
    return temperatura, False


def _detectar_analise_suspeita(
    classifier_reason: str | None,
    messages: list[dict[str, Any]],
    deep: Any | None,
) -> bool:
    """
    Heurística barata de coerência classifier×analyzer (I6.2).

    Marca suspeita quando o classifier sinaliza que o lead não respondeu / 0
    respostas, mas o analyzer produziu resumo com diálogo (ou vice-versa: deep
    presente com 0 msgs do lead). Teria pego o bug da conversa trocada (I1).
    """
    n_lead = sum(1 for m in messages if m.get("from") == "lead")
    reason = (classifier_reason or "").lower()
    sinais_sem_resposta = any(
        s in reason for s in (
            "não respondeu", "nao respondeu", "sem resposta",
            "0 resposta", "não houve troca", "nao houve troca",
            "lead não falou", "lead nao falou",
        )
    )
    tem_deep = bool(deep and getattr(deep, "conversa_resumo", None))
    # classifier diz "sem resposta" mas analyzer reconstruiu diálogo com lead falando
    if sinais_sem_resposta and tem_deep and n_lead > 0:
        return True
    # deep análise presente mas nenhuma msg do lead na conversa vista
    if tem_deep and n_lead == 0:
        return True
    return False


def _validar_ultima_msg_meta(
    ultima_at: str | None,
    ultima_de: str | None,
    messages: list[dict[str, Any]],
) -> tuple[str | None, str | None, bool]:
    """
    Valida ultima_msg_at/de contra o array real de mensagens (I6.3).

    Retorna (at_correto, de_correto, divergiu?). Corrige se divergir.
    """
    real_at, real_de = _last_message_meta(messages)
    divergiu = (real_at != ultima_at) or (real_de != ultima_de)
    return real_at, real_de, divergiu


def _parse_tasks_for_lead(
    overdue_tasks: list[dict[str, Any]],
    lead_id: str,
) -> list[str]:
    """Retorna lista de textos de tarefas atrasadas para um lead (para exibição)."""
    result = []
    for t in overdue_tasks:
        if str(t.get("entity_id", "")) == str(lead_id):
            text = t.get("tipo") or t.get("text") or f"Tarefa #{t.get('task_id')}"
            result.append(text)
    return result


def _get_lead_tasks_detail(
    overdue_tasks: list[dict[str, Any]],
    lead_id: str,
) -> list[dict[str, Any]]:
    """Retorna detalhe estruturado das tarefas atrasadas de um lead específico."""
    result = []
    for t in overdue_tasks:
        if str(t.get("entity_id", "")) != str(lead_id):
            continue
        result.append({
            "tipo": t.get("tipo") or t.get("text") or "follow-up",
            "vencida_em": str(t.get("complete_till_at") or t.get("due_at") or ""),
            "dias_atrasada": int(t.get("dias_atrasada", 0)),
        })
    return result


def _infer_temperatura_from_tasks(tasks_detail: list[dict[str, Any]]) -> str:
    """
    Infere temperatura a partir de tarefas atrasadas:
      1-2 dias → morno  |  3+ dias → frio
    """
    if not tasks_detail:
        return "frio"
    max_dias = max(t.get("dias_atrasada", 0) for t in tasks_detail)
    return "morno" if max_dias <= 2 else "frio"


def _build_gap_tarefa(tasks_detail: list[dict[str, Any]]) -> str:
    """Gap determinístico para modo tarefa_atrasada."""
    worst = max(tasks_detail, key=lambda t: t.get("dias_atrasada", 0))
    dias = worst.get("dias_atrasada", 0)
    return f"Tarefa de follow-up vencida há {dias} dia(s), sem contato registrado."


def _build_sugestao_tarefa(lead_nome: str, tasks_detail: list[dict[str, Any]]) -> str:
    """Sugestão determinística para modo tarefa_atrasada (zero LLM)."""
    n = len(tasks_detail)
    worst = max(tasks_detail, key=lambda t: t.get("dias_atrasada", 0))
    dias = worst.get("dias_atrasada", 0)
    tipo = worst.get("tipo", "follow-up")
    urgencia = " ⚠️ URGENTE — prioridade máxima." if dias >= 6 else "."
    return (
        f"Você tem {n} tarefa(s) atrasada(s) com {lead_nome}: "
        f"{tipo} vencida há {dias} dia(s). "
        f"Sugestão: priorizar contato hoje{urgencia}"
    )


# ── Cache (Fase 2 — Inspector v2) ─────────────────────────────────────────────

def build_cache_maps(
    lead_ids: list[str],
    corretora_id: int,
    jids_por_lead: dict[str, list[str]],
) -> tuple[dict[str, datetime], dict[str, datetime]]:
    """
    Pre-fetch em batch dos dois maps usados em ``should_reanalyze``.

    Retorna:
      • ``last_analyzed_map``  — lead_id → MAX(lead_analyses.analyzed_at)
      • ``last_msg_map``       — remote_jid → MAX(mensagens_whatsapp.timestamp_evolution)

    Substitui as 2 queries por lead por 2 queries no total (GROUP BY).
    Pra Acme (~150 leads): 300 → 2 queries.
    """
    last_analyzed_map: dict[str, datetime] = {}
    last_msg_map: dict[str, datetime] = {}

    if lead_ids:
        with SessionLocal() as s:
            rows = s.execute(
                text(
                    "SELECT lead_id, MAX(analyzed_at) "
                    "FROM lead_analyses "
                    "WHERE lead_id = ANY(:lids) "
                    "GROUP BY lead_id"
                ),
                {"lids": [str(x) for x in lead_ids]},
            ).all()
        for lid, last_at in rows:
            if last_at is not None:
                last_analyzed_map[str(lid)] = last_at

    all_jids: set[str] = set()
    for jids in jids_por_lead.values():
        all_jids.update(jids)

    if all_jids:
        with SessionLocal() as s:
            rows = s.execute(
                text(
                    "SELECT remote_jid, MAX(timestamp_evolution) "
                    "FROM mensagens_whatsapp "
                    "WHERE corretora_id = :cid AND remote_jid = ANY(:jids) "
                    "GROUP BY remote_jid"
                ),
                {"cid": corretora_id, "jids": list(all_jids)},
            ).all()
        for jid, last_msg in rows:
            if last_msg is not None:
                last_msg_map[str(jid)] = last_msg

    return last_analyzed_map, last_msg_map


def should_reanalyze(
    lead_id: str,
    lead_phone: str,
    corretora_id: int,
    instance: str,
    evolution: EvolutionTool,
    jid_map: dict[str, str] | None = None,
    last_analyzed_map: dict[str, datetime] | None = None,
    last_msg_map: dict[str, datetime] | None = None,
) -> bool:
    """
    Cache check: True se vale reanalisar este lead.

    Reanalisa quando:
      • Nunca foi analisado (sem registro em lead_analyses), OU
      • MAX(mensagens_whatsapp.timestamp_evolution) > MAX(lead_analyses.analyzed_at)

    Caso contrário (já analisado e sem msg nova): retorna False (cache hit).

    Tenta JID @s.whatsapp.net primeiro e faz fallback via _find_jid_by_phone
    pra cobrir formato @lid. Mesmo padrão de conversation_reader.

    Otimizações opcionais (Wave 1+2):
      • ``jid_map``           — dict ``digits → jid`` via ``build_jid_lookup_map``
      • ``last_analyzed_map`` — dict ``lead_id → datetime`` via ``build_cache_maps``
      • ``last_msg_map``      — dict ``remote_jid → datetime`` via ``build_cache_maps``
    Quando passados, evitam queries por lead (degrada graciosamente pra
    SELECT individual quando map ausente — útil em testes).
    """
    if last_analyzed_map is not None:
        last_analyzed = last_analyzed_map.get(str(lead_id))
    else:
        with SessionLocal() as s:
            last_analyzed = s.execute(
                text(
                    "SELECT MAX(analyzed_at) FROM lead_analyses WHERE lead_id = :lid"
                ),
                {"lid": str(lead_id)},
            ).scalar()

    if last_analyzed is None:
        log.info("inspector_cache_miss", lead_id=lead_id, reason="never_analyzed")
        return True

    digits = _normalize_phone(lead_phone or "")
    if not digits:
        log.info("inspector_cache_miss", lead_id=lead_id, reason="no_phone")
        return True

    jids: list[str] = [f"{digits}@s.whatsapp.net"]
    lid_jid: str | None = None
    if jid_map is not None:
        mapped = jid_map.get(digits)
        if mapped and mapped != jids[0]:
            lid_jid = mapped
    else:
        try:
            lid_jid = evolution._find_jid_by_phone(instance, digits)
        except Exception:
            log.exception("inspector_cache_jid_resolve_failed", lead_id=lead_id)
            lid_jid = None
    if lid_jid and lid_jid not in jids:
        jids.append(lid_jid)

    if last_msg_map is not None:
        last_msg = None
        for j in jids:
            candidate = last_msg_map.get(j)
            if candidate and (last_msg is None or candidate > last_msg):
                last_msg = candidate
    else:
        with SessionLocal() as s:
            last_msg = s.execute(
                text(
                    "SELECT MAX(timestamp_evolution) FROM mensagens_whatsapp "
                    "WHERE corretora_id = :cid AND remote_jid = ANY(:jids)"
                ),
                {"cid": corretora_id, "jids": jids},
            ).scalar()

    if last_msg is None:
        log.info(
            "inspector_cache_hit",
            lead_id=lead_id,
            reason="no_messages_since_last_analysis",
        )
        return False

    if last_analyzed.tzinfo is None:
        last_analyzed = last_analyzed.replace(tzinfo=timezone.utc)
    if last_msg.tzinfo is None:
        last_msg = last_msg.replace(tzinfo=timezone.utc)

    if last_msg > last_analyzed:
        log.info(
            "inspector_cache_miss",
            lead_id=lead_id,
            reason="new_messages",
            last_msg=last_msg.isoformat(),
            last_analyzed=last_analyzed.isoformat(),
        )
        return True

    log.info(
        "inspector_cache_hit",
        lead_id=lead_id,
        reason="up_to_date",
        last_msg=last_msg.isoformat(),
        last_analyzed=last_analyzed.isoformat(),
    )
    return False


# ── Main pipeline ──────────────────────────────────────────────────────────────

def run_daily_inspection(
    only_corretora_id: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Executa inspeção diária de leads em 4 modos:

    1. conversa_whatsapp — tem msgs pós-conexão → Classifier + Analyzer (LLM)
    2. tarefa_atrasada   — sem msgs, mas tarefa Kommo vencida → determinístico
    3. sem_atividade     — nem msgs nem tarefas → frio sem ação
    4. sem_dado_historico — conexão Evolution posterior à atividade → sem dado

    Args:
        only_corretora_id: processa apenas esta corretora (MVP: 3=Pessoa3)
        dry_run: se True, não grava em lead_analyses

    Returns:
        dict com contagens, by_status, by_temperatura, by_mode, tokens, analyses
    """
    stats: dict[str, Any] = {
        "total_leads": 0,
        "processed": 0,
        "deep_analyzed": 0,
        "deep_teto_atingido": False,
        "llm_calls": 0,
        "deterministic": 0,
        "skipped_no_phone": 0,
        "skipped_sem_dado": 0,
        "skipped_interno": 0,
        "cache_hits": 0,
        "errors": 0,
        "by_status": {
            "atendido": 0,
            "nao_atendido": 0,
            "sem_resposta": 0,
            "atendimento_pendente": 0,
            "sem_dado_historico": 0,
            "erro": 0,
        },
        "by_temperatura": {"frio": 0, "morno": 0, "quente": 0},
        "by_mode": {
            "conversa_whatsapp": 0,
            "tarefa_atrasada": 0,
            "sem_atividade": 0,
            "sem_dado_historico": 0,
        },
        "tokens_classifier": 0,
        "tokens_analyzer": 0,
        "analyses": [],
    }

    # Pré-flush: tenta transcrever áudios pendentes antes do pipeline ler conversa.
    # Reduz "[áudio pendente]" no contexto LLM. Cap em 48h pra não tentar mídia já expirada.
    #
    # O teto era 50, e este era o ÚNICO lugar que transcrevia áudio novo — 1x
    # por dia. Chegam ~130 áudios/dia, então a fila crescia 80 por dia, o
    # excedente passava das 48h e nunca mais era olhado: 8.308 acumulados até
    # 25/08/2026, metade da base. O trabalho de verdade agora é do timer
    # copiloto-transcricao-backfill (a cada 15 min); este pré-flush virou só uma
    # última varrida antes do pipeline ler as conversas — mas com teto que
    # cabe um dia inteiro, pra não voltar a ser o gargalo se o timer falhar.
    try:
        from app.services.transcricao_service import transcrever_pendentes
        flush_stats = transcrever_pendentes(limit=300, max_idade_horas=48)
        log.info("pipeline_pre_flush_transcricoes", **flush_stats)
    except Exception:
        log.exception("pipeline_pre_flush_failed")

    # KommoTool saiu do Inspector em 25/08/2026: leads, etapas e tarefas agora
    # vêm todos do banco próprio. O Kommo não recebe lead desde 05/08 — manter
    # a chamada só reintroduzia dado congelado no briefing.
    evolution = EvolutionTool()

    # Etapas vêm de `pipeline_stages` (banco próprio), não mais do Kommo.
    # `leads.stage_id` usa ids curtos (6..17) do funil novo; os ids do Kommo
    # têm 8 dígitos. Com o mapa velho TODA etapa de lead novo vinha vazia.
    try:
        with SessionLocal() as _s:
            stage_map: dict[str, str] = {
                str(r[0]): r[1]
                for r in _s.execute(
                    text("select id, nome from pipeline_stages where tenant_id = 1")
                ).all()
            }
    except Exception:
        log.exception("pipeline_stage_map_failed")
        stage_map = {}

    with SessionLocal() as session:
        q = session.query(Corretora).filter(
            Corretora.ativo == True,
            Corretora.evolution_instance != None,
            Corretora.kommo_user_id != None,
        )
        if only_corretora_id is not None:
            q = q.filter(Corretora.id == only_corretora_id)
        corretoras = q.all()
        corretora_rows = [
            {
                "id": c.id,
                "nome": c.nome,
                "evolution_instance": c.evolution_instance,
                "kommo_user_id": c.kommo_user_id,
                "evolution_connected_since": c.evolution_connected_since,
                "canal": getattr(c, "canal", None) or "evolution",
            }
            for c in corretoras
        ]

    internos_phones = _load_internos_set()
    log.info("pipeline_start", corretoras=len(corretora_rows), dry_run=dry_run,
             internos=len(internos_phones))

    # ── Detecção de automação por corretora (Onda 3a.2) ──────────────────────
    # Roda ANTES dos leads pra coaching já saber filtrar. Falha não quebra.
    if not dry_run:
        from app.services.deteccao_automacao import (
            detectar_automacoes, upsert_automacoes, invalidar_cache,
        )
        for c in corretora_rows:
            try:
                cand = detectar_automacoes(c["id"])
                upsert_automacoes(c["id"], cand)
                invalidar_cache(c["id"])
                log.info("pipeline_automacao_ok", corretora_id=c["id"],
                         candidatos=len(cand))
            except Exception:
                log.exception("pipeline_automacao_failed", corretora_id=c["id"])

    for corretora in corretora_rows:
        corretora_id = corretora["id"]
        corretora_nome = corretora["nome"]

        log.info(
            "pipeline_corretora_start",
            corretora_id=corretora_id,
            corretora=corretora_nome,
        )

        # ── Advisory lock: uma inspeção por corretora por vez ────────────────
        # Se outra sessão já processa esta corretora, pula (evita duplicar
        # chamadas LLM = custo real). Trava de sessão liberada no finally.
        _lock_conn = _try_acquire_corretora_lock(corretora_id)
        if _lock_conn is None:
            log.warning(
                "pipeline_corretora_lock_ocupada",
                corretora_id=corretora_id,
                corretora=corretora_nome,
                motivo="ja_rodando_concorrente",
            )
            stats.setdefault("skipped_lock", []).append({
                "corretora_id": corretora_id,
                "corretora": corretora_nome,
            })
            continue

        try:
            _process_corretora(
                corretora=corretora,
                stats=stats,
                evolution=evolution,
                stage_map=stage_map,
                internos_phones=internos_phones,
                dry_run=dry_run,
            )
        finally:
            _release_corretora_lock(_lock_conn, corretora_id)

    _finalize_pipeline(stats, dry_run)
    return stats


def _process_corretora(
    corretora: dict[str, Any],
    stats: dict[str, Any],
    evolution: "EvolutionTool",
    stage_map: dict[str, str],
    internos_phones: set[str],
    dry_run: bool,
) -> None:
    """
    Processa uma única corretora (extraído de run_daily_inspection pra permitir
    o wrap try/finally do advisory lock sem re-indentar todo o corpo).

    Toda a lógica de janela/leads/coaching/run vive aqui; muta ``stats`` in-place.
    """
    corretora_id = corretora["id"]
    corretora_nome = corretora["nome"]
    instance = corretora["evolution_instance"]
    kommo_user_id = corretora["kommo_user_id"]
    connected_since = corretora["evolution_connected_since"]
    # Canal da fonte de verdade: 'cloud' (Pessoa12 na Meta Cloud API — lê da tabela
    # mensagens_whatsapp) ou 'evolution' (demais — lê pela instância Evolution).
    canal = corretora.get("canal") or "evolution"
    is_cloud = canal == "cloud"

    if True:  # bloco preservado com indent original (evita re-indentar ~400 linhas)
        # ── Inspector run: janela dinâmica ───────────────────────────────────
        # Marca runs órfãs (crash anterior), calcula janela, registra nova run.
        _mark_orphan_runs(corretora_id)
        last_run_finished = _get_last_run_finished_at(corretora_id)
        window_start = _compute_window_start(last_run_finished, connected_since)
        run_id = _create_inspector_run(corretora_id, window_start)
        run_msgs = 0
        run_leads = 0
        log.info(
            "pipeline_run_created",
            corretora_id=corretora_id,
            run_id=str(run_id),
            window_start=window_start.isoformat(),
            first_run=last_run_finished is None,
        )

        # ── Guard: instância Evolution offline ──────────────────────────────
        # Canal 'cloud' (Pessoa12) NÃO usa a instância Evolution: a conversa vive na
        # tabela mensagens_whatsapp (webhook cloud_corretora_a). Pular o check
        # evita o falso "pipeline_instancia_offline" (a instância dela fica
        # status=close DE PROPÓSITO) e a leitura HTTP inútil.
        jid_map: dict[str, str] = {}
        if is_cloud:
            instance_status = "cloud"
            log.info(
                "pipeline_canal_cloud",
                corretora=corretora_nome,
                instance=instance,
            )
        else:
            instance_status = "unknown"
            try:
                instance_status = evolution.check_instance_status(instance)
            except Exception:
                log.exception("pipeline_instance_status_failed", instance=instance)
            if instance_status != "open":
                log.warning(
                    "pipeline_instancia_offline",
                    corretora=corretora_nome,
                    instance=instance,
                    status=instance_status,
                )
                stats.setdefault("instancias_offline", []).append({
                    "corretora": corretora_nome,
                    "instance": instance,
                    "status": instance_status,
                })

            # ── Bulk JID lookup: 1 chamada a find_chats por corretora ───────
            # Evita N chamadas dentro do loop (was: _find_jid_by_phone por lead).
            if instance_status == "open":
                try:
                    _raw_map = evolution.build_jid_lookup_map(instance)
                    if isinstance(_raw_map, dict):
                        jid_map = _raw_map
                except Exception:
                    log.exception("pipeline_build_jid_map_failed", instance=instance)
                    jid_map = {}

        # Tarefas atrasadas vinham do Kommo, que a operação abandonou em
        # 05/08/2026. O que sobrou lá são 190 tarefas vencidas de lead antigo,
        # que ninguém vai executar — jogar isso no briefing é inventar pendência
        # e treinar a corretora a ignorar a seção. Fica vazio até existir fonte
        # de tarefa viva no banco próprio.
        overdue_tasks: list[dict[str, Any]] = []

        # ── Fonte dos leads: banco próprio, NÃO o Kommo ─────────────────────
        # O Kommo parou de receber lead em 05/08/2026 — a entrada passou pro
        # webhook meta_leadgen, que grava em public.leads. O Inspector ficou
        # perguntando pra fonte congelada e, em 25/08, tinha 127 leads novos
        # com ZERO análise (93 deles já haviam respondido a corretora).
        # `fonte_leads` devolve o mesmo schema achatado e já vem deduplicado.
        try:
            leads: list[dict[str, Any]] = get_leads_for_inspector_db(corretora_id)
        except Exception:
            log.exception("pipeline_fonte_leads_failed", corretora_id=corretora_id)
            _fail_inspector_run(run_id, "fonte_leads_failed")
            return

        _por_motivo: dict[str, int] = {}
        for _l in leads:
            _k = str(_l.get("entrou_por") or "?")
            _por_motivo[_k] = _por_motivo.get(_k, 0) + 1
        log.info(
            "pipeline_leads_unificados",
            corretora_id=corretora_id,
            total=len(leads),
            por_motivo=_por_motivo,
        )
        stats["total_leads"] += len(leads)

        # ── Cache maps em batch (Wave 2) ────────────────────────────────────
        # 2 queries GROUP BY pra toda a corretora, ao invés de 2 por lead.
        jids_por_lead: dict[str, list[str]] = {}
        for _lead in leads:
            _digits = _normalize_phone(_lead.get("telefone") or "")
            if not _digits:
                continue
            _jids = [f"{_digits}@s.whatsapp.net"]
            _mapped = jid_map.get(_digits)
            if _mapped and _mapped not in _jids:
                _jids.append(_mapped)
            jids_por_lead[str(_lead["lead_id"])] = _jids
        try:
            last_analyzed_map, last_msg_map = build_cache_maps(
                lead_ids=[str(l["lead_id"]) for l in leads],
                corretora_id=corretora_id,
                jids_por_lead=jids_por_lead,
            )
        except Exception:
            log.exception("pipeline_build_cache_maps_failed", corretora_id=corretora_id)
            last_analyzed_map, last_msg_map = {}, {}

        # Coletado durante o loop de leads dessa corretora — usado pra coaching
        # ao final (apenas leads com mensagens). Cada item:
        #   {lead_id, lead_nome, temperatura, messages}
        coaching_pool: list[dict[str, Any]] = []

        for lead in leads:
            lead_id = lead["lead_id"]
            lead_nome = lead.get("nome") or "?"
            lead_telefone = lead.get("telefone") or ""
            entrou_por = lead.get("entrou_por")
            etapa_nome = stage_map.get(str(lead.get("etapa_id") or ""), str(lead.get("etapa_id") or ""))
            lead["etapa_nome"] = etapa_nome

            try:
                _upsert_snapshot(lead, corretora_id)

                # ── Dual-write EM SOMBRA da espinha canônica (Opção A) ────────
                # Best-effort: falha aqui NÃO pode quebrar o Inspector. Leitura
                # segue no Kommo; só gravamos em paralelo (settings.lead_spine_shadow).
                try:
                    from app.services.lead_spine import upsert_lead_shadow
                    upsert_lead_shadow({**lead, "corretora_id": corretora_id})
                except Exception:
                    log.exception("lead_spine_shadow_failed", lead_id=lead_id)

                # ── Guard: interno Acme (não é lead real) ───────────────────
                if lead_telefone and _normalize_phone(lead_telefone) in internos_phones:
                    log.info(
                        "pipeline_skip_interno",
                        lead_id=lead_id,
                        phone_suffix=lead_telefone[-4:],
                    )
                    stats["skipped_interno"] += 1
                    continue

                # ── Guard: sem telefone ───────────────────────────────────────
                if not lead_telefone:
                    log.warning("pipeline_skip_no_phone", lead_id=lead_id)
                    stats["skipped_no_phone"] += 1
                    output = LeadAnalysisOutput(
                        lead_id=lead_id, lead_name=lead_nome,
                        corretora=corretora_nome, corretora_id=corretora_id, entrou_por=entrou_por,
                        analysis_mode="sem_atividade",
                        status="nao_atendido", temperatura="frio",
                        ultima_msg_at=None, ultima_msg_de=None,
                        conversa_resumo=None, o_que_corretora_disse=None,
                        gap_identificado=None, abordagem_sugerida=None,
                        tarefas_kommo_atrasadas=_parse_tasks_for_lead(overdue_tasks, lead_id),
                        etapa_kommo_desatualizada=False,
                        skipped_reason="no_phone",
                    )
                    _finalize(output, stats, dry_run)
                    continue

                # ── Cache check (Fase 2): pula se conversa não mudou ──────────
                # Codex #8: o cache é baseado só em timestamp de MENSAGEM
                # (mensagens_whatsapp × lead_analyses.analyzed_at) — não sabe
                # nada sobre tarefa Kommo atrasada. Tarefas são buscadas frescas
                # a cada run (ficam mais atrasadas, ou surgem novas) e não fazem
                # parte da chave do cache; sem este bypass, um lead sem mensagem
                # (analisado 1x como sem_atividade/tarefa_atrasada) ficava preso
                # no cache PRA SEMPRE, mesmo que uma tarefa atrasada aparecesse
                # depois. O modo tarefa_atrasada é determinístico (zero LLM),
                # então o bypass não tem custo de chamada LLM.
                tarefas_precheck = _get_lead_tasks_detail(overdue_tasks, lead_id)
                if not tarefas_precheck and not should_reanalyze(
                    lead_id, lead_telefone, corretora_id, instance, evolution,
                    jid_map=jid_map,
                    last_analyzed_map=last_analyzed_map,
                    last_msg_map=last_msg_map,
                ):
                    stats["cache_hits"] += 1
                    continue
                if tarefas_precheck:
                    log.info(
                        "inspector_cache_bypass_tarefa_atrasada",
                        lead_id=lead_id,
                        n_tasks=len(tarefas_precheck),
                    )
                    stats["cache_bypass_tarefa_atrasada"] = (
                        stats.get("cache_bypass_tarefa_atrasada", 0) + 1
                    )

                # ── Busca conversa Postgres (mensagens_whatsapp) ─────────────
                # Fase 4 + fix 7.1: busca TODAS as mensagens disponíveis no
                # banco (cutoff=None) e deixa _apply_connection_window separar
                # pre/pos-conexão. Sem isso, leads cuja conversa toda é pré-
                # conexão retornam raw_messages vazio e caem em sem_atividade
                # em vez de sem_dado_historico.
                if is_cloud:
                    # Canal 'cloud': a conversa vem da TABELA mensagens_whatsapp
                    # (gravada pelos webhooks cloud_corretora_a/cloud_corretora). Lê
                    # tudo (720h teto do ler_thread_cloud) e deixa
                    # _apply_connection_window decidir o pré/pós-conexão, igual ao
                    # ramo Evolution. O corretora_id é o que separa a thread da
                    # dona — sem ele, só a Pessoa12 enxergava conversa.
                    raw_messages = _ler_conversa_cloud(lead_telefone, corretora_id)
                else:
                    raw_messages = get_conversation_db(
                        corretora_id, lead_telefone, instance, evolution,
                        hours_back=0,  # sem cutoff — _apply_connection_window decide
                    )
                run_msgs += len(raw_messages)
                messages = _apply_connection_window(raw_messages, connected_since)

                # Trunca pras últimas 200 mensagens (proteção de contexto)
                if len(messages) > 200:
                    log.info(
                        "pipeline_messages_truncated",
                        lead_id=lead_id,
                        original=len(messages),
                        kept=200,
                    )
                    messages = messages[-200:]

                # ── Modo: sem_dado_historico ──────────────────────────────────
                if not messages and raw_messages:
                    log.info(
                        "pipeline_skip_pre_connection",
                        lead_id=lead_id,
                        raw_count=len(raw_messages),
                    )
                    stats["skipped_sem_dado"] += 1
                    output = LeadAnalysisOutput(
                        lead_id=lead_id, lead_name=lead_nome,
                        corretora=corretora_nome, corretora_id=corretora_id, entrou_por=entrou_por,
                        analysis_mode="sem_dado_historico",
                        status="sem_dado_historico", temperatura="frio",
                        ultima_msg_at=None, ultima_msg_de=None,
                        conversa_resumo=None, o_que_corretora_disse=None,
                        gap_identificado=None, abordagem_sugerida=None,
                        tarefas_kommo_atrasadas=_parse_tasks_for_lead(overdue_tasks, lead_id),
                        etapa_kommo_desatualizada=False,
                        skipped_reason="evolution_connected_after_lead_activity",
                    )
                    _finalize(output, stats, dry_run)
                    continue

                # ── Modo: sem_atividade_hoje ─────────────────────────────────
                # Pula análise LLM quando a conversa existe mas a última msg
                # não é de hoje SP — evita gerar coaching sobre dados velhos.
                # `parado` e `lead_novo` NÃO passam por esta trava: um lead
                # parado, por definição, não falou hoje — falou dias atrás e
                # ninguém respondeu. Era esse o ponto de trazê-lo. Antes disso
                # a trava descartava 30 de 48 leads e ainda carimbava
                # temperatura="frio" sem ler a conversa — foi assim que 697
                # leads por semana viraram "frio" sem análise nenhuma.
                _isento_da_trava = entrou_por in ("parado", "lead_novo")
                if messages and not _isento_da_trava and not _last_message_is_today_sp(messages):
                    ultima_at, ultima_de = _last_message_meta(messages)
                    log.info(
                        "pipeline_skip_sem_atividade_hoje",
                        lead_id=lead_id,
                        ultima_msg_at=ultima_at,
                    )
                    output = LeadAnalysisOutput(
                        lead_id=lead_id, lead_name=lead_nome,
                        corretora=corretora_nome, corretora_id=corretora_id, entrou_por=entrou_por,
                        analysis_mode="sem_atividade_hoje",
                        status="sem_atividade_hoje", temperatura="frio",
                        ultima_msg_at=ultima_at, ultima_msg_de=ultima_de,
                        conversa_resumo=None, o_que_corretora_disse=None,
                        gap_identificado=None, abordagem_sugerida=None,
                        tarefas_kommo_atrasadas=_parse_tasks_for_lead(overdue_tasks, lead_id),
                        etapa_kommo_desatualizada=False,
                        skipped_reason="no_activity_today",
                    )
                    _finalize(output, stats, dry_run)
                    continue

                # ── Modo: conversa_whatsapp ───────────────────────────────────
                if messages:
                    run_leads += 1
                    classification: ClassificationResult = classify_lead(lead, messages)
                    stats["tokens_classifier"] += classification.tokens_used
                    stats["llm_calls"] += 1

                    # ── I6.3: valida/corrige ultima_msg_at/de contra o array ──
                    raw_ultima_at, raw_ultima_de = _last_message_meta(messages)
                    ultima_at, ultima_de, meta_divergiu = _validar_ultima_msg_meta(
                        raw_ultima_at, raw_ultima_de, messages
                    )
                    if meta_divergiu:
                        log.warning(
                            "ultima_msg_meta_corrigida", lead_id=lead_id,
                            de=raw_ultima_de, para=ultima_de,
                        )
                    tarefas_str = _parse_tasks_for_lead(overdue_tasks, lead_id)

                    # ── I4: staleness — dias desde a última msg DO LEAD ───────
                    dias_silencio = dias_sem_msg_lead(messages)

                    # ── Análise profunda: todo lead com conversa (26/08/2026) ──
                    #
                    # Antes, `deserves_deep_analysis=False` vetava o Sonnet e o
                    # lead sumia do briefing das 20h, do coaching da corretora e
                    # do radar da Dona — sem deixar rastro. Medido em 26/08: 40
                    # leads COM conversa descartados assim em 7 dias, num volume
                    # de 43-94 leads/dia. A economia não pagava o lead perdido.
                    #
                    # O veto do classifier agora é só registrado.
                    deep: DeepAnalysisResult | None = None
                    teria_descartado = not classification.deserves_deep_analysis
                    if stats["deep_analyzed"] >= MAX_DEEP_ANALISES_POR_RODADA:
                        if not stats["deep_teto_atingido"]:
                            stats["deep_teto_atingido"] = True
                            log.warning(
                                "deep_teto_atingido",
                                teto=MAX_DEEP_ANALISES_POR_RODADA,
                                corretora_id=corretora_id,
                            )
                    else:
                        deep = analyze_lead(
                            lead, messages, corretora_nome,
                            dias_silencio_lead=dias_silencio,
                        )
                        if deep:
                            stats["tokens_analyzer"] += deep.tokens_used
                            stats["deep_analyzed"] += 1

                    # ── I4.2: cap de temperatura por silêncio (pós-classifier) ─
                    temperatura, temp_capped = _cap_temperatura_staleness(
                        classification.temperatura, dias_silencio
                    )
                    if temp_capped:
                        log.info(
                            "temperatura_capped_staleness", lead_id=lead_id,
                            de=classification.temperatura, para=temperatura,
                            dias_silencio=dias_silencio,
                        )

                    # ── I6.2: coerência classifier×analyzer ───────────────────
                    analise_suspeita = _detectar_analise_suspeita(
                        classification.reason, messages, deep
                    )
                    if analise_suspeita:
                        log.warning(
                            "analise_suspeita", lead_id=lead_id,
                            classifier_reason=classification.reason,
                            n_lead=sum(1 for m in messages if m.get("from") == "lead"),
                        )

                    output = LeadAnalysisOutput(
                        lead_id=lead_id, lead_name=lead_nome,
                        corretora=corretora_nome, corretora_id=corretora_id, entrou_por=entrou_por,
                        analysis_mode="conversa_whatsapp",
                        status=classification.status,
                        temperatura=temperatura,
                        ultima_msg_at=ultima_at, ultima_msg_de=ultima_de,
                        conversa_resumo=deep.conversa_resumo if deep else None,
                        o_que_corretora_disse=deep.o_que_corretora_disse if deep else None,
                        gap_identificado=deep.gap_identificado if deep else None,
                        abordagem_sugerida=deep.abordagem_sugerida if deep else None,
                        tarefas_kommo_atrasadas=tarefas_str,
                        etapa_kommo_desatualizada=False,
                        skipped_reason=None,
                        classifier_reason=classification.reason,
                        classifier_teria_descartado=teria_descartado,
                        tokens_classifier=classification.tokens_used,
                        tokens_analyzer=deep.tokens_used if deep else 0,
                        dias_sem_msg_lead=dias_silencio,
                        temperatura_capped_staleness=temp_capped,
                        citacao_verificada=deep.citacao_verificada if deep else True,
                        respiro_valido=deep.respiro_valido if deep else True,
                        analise_suspeita=analise_suspeita,
                    )
                    _finalize(output, stats, dry_run)
                    coaching_pool.append({
                        "lead_id": lead_id,
                        "lead_nome": lead_nome,
                        "temperatura": temperatura,
                        "messages": messages,
                    })
                    continue

                # ── Sem mensagens: verificar tarefas ─────────────────────────
                tasks_detail = _get_lead_tasks_detail(overdue_tasks, lead_id)
                tarefas_str = _parse_tasks_for_lead(overdue_tasks, lead_id)

                # ── Modo: tarefa_atrasada ─────────────────────────────────────
                if tasks_detail:
                    temperatura = _infer_temperatura_from_tasks(tasks_detail)
                    gap = _build_gap_tarefa(tasks_detail)
                    sugestao = _build_sugestao_tarefa(lead_nome, tasks_detail)

                    log.info(
                        "pipeline_tarefa_atrasada",
                        lead_id=lead_id,
                        n_tasks=len(tasks_detail),
                        max_dias=max(t.get("dias_atrasada", 0) for t in tasks_detail),
                    )
                    stats["deterministic"] += 1

                    output = LeadAnalysisOutput(
                        lead_id=lead_id, lead_name=lead_nome,
                        corretora=corretora_nome, corretora_id=corretora_id, entrou_por=entrou_por,
                        analysis_mode="tarefa_atrasada",
                        status="atendimento_pendente", temperatura=temperatura,
                        ultima_msg_at=None, ultima_msg_de=None,
                        conversa_resumo=None, o_que_corretora_disse=None,
                        gap_identificado=gap,
                        abordagem_sugerida=sugestao,
                        tarefas_kommo_atrasadas=tarefas_str,
                        tarefas_atrasadas_detail=tasks_detail,
                        etapa_kommo_desatualizada=False,
                    )
                    _finalize(output, stats, dry_run)
                    continue

                # ── Modo: sem_atividade ───────────────────────────────────────
                log.info("pipeline_sem_atividade", lead_id=lead_id)
                stats["deterministic"] += 1

                output = LeadAnalysisOutput(
                    lead_id=lead_id, lead_name=lead_nome,
                    corretora=corretora_nome, corretora_id=corretora_id, entrou_por=entrou_por,
                    analysis_mode="sem_atividade",
                    status="nao_atendido", temperatura="frio",
                    ultima_msg_at=None, ultima_msg_de=None,
                    conversa_resumo=None, o_que_corretora_disse=None,
                    gap_identificado=None, abordagem_sugerida=None,
                    tarefas_kommo_atrasadas=tarefas_str,
                    etapa_kommo_desatualizada=False,
                )
                _finalize(output, stats, dry_run)

            except Exception:
                log.exception("pipeline_lead_error", lead_id=lead_id)
                stats["errors"] += 1
                stats["by_status"]["erro"] += 1
                error_output = LeadAnalysisOutput(
                    lead_id=lead_id, lead_name=lead_nome,
                    corretora=corretora_nome, corretora_id=corretora_id, entrou_por=entrou_por,
                    analysis_mode="sem_atividade",
                    status="erro", temperatura="frio",
                    ultima_msg_at=None, ultima_msg_de=None,
                    conversa_resumo=None, o_que_corretora_disse=None,
                    gap_identificado=None, abordagem_sugerida=None,
                    tarefas_kommo_atrasadas=[],
                    etapa_kommo_desatualizada=False,
                    skipped_reason="pipeline_error",
                )
                if not dry_run:
                    try:
                        _save_analysis(error_output)
                    except Exception:
                        log.exception("pipeline_save_error_record_failed", lead_id=lead_id)

        # ── Coaching da corretora (Onda 3a.1) ──────────────────────────────
        # Top 3 quentes do dia + Top 2 que esfriaram (morno→frio em 7d).
        # Falha NÃO quebra Inspector.
        if not dry_run:
            try:
                n = _gerar_coaching_corretora(
                    corretora_id=corretora_id,
                    corretora_nome=corretora_nome,
                    coaching_pool=coaching_pool,
                )
                stats.setdefault("coaching", {})[corretora_nome] = n
            except Exception:
                log.exception("pipeline_coaching_failed", corretora_id=corretora_id)
                stats.setdefault("coaching", {})[corretora_nome] = "error"

        # ── Completa inspector_run ────────────────────────────────────────
        try:
            _complete_inspector_run(run_id, run_msgs, run_leads)
            log.info(
                "pipeline_run_completed",
                corretora_id=corretora_id,
                run_id=str(run_id),
                mensagens=run_msgs,
                leads=run_leads,
            )
        except Exception:
            log.exception("pipeline_run_complete_failed", run_id=str(run_id))


def _finalize_pipeline(stats: dict[str, Any], dry_run: bool) -> None:
    """
    Agregações pós-loop (tokens, consolidação, funil, snapshots).

    Extraído de run_daily_inspection junto com _process_corretora. Muta
    ``stats`` in-place. Cada bloco é isolado — falha NÃO quebra o pipeline.
    """
    stats["tokens"] = {
        "classifier": stats["tokens_classifier"],
        "analyzer": stats["tokens_analyzer"],
        "total": stats["tokens_classifier"] + stats["tokens_analyzer"],
    }

    log.info(
        "pipeline_complete",
        total=stats["total_leads"],
        processed=stats["processed"],
        deep_analyzed=stats["deep_analyzed"],
        llm_calls=stats["llm_calls"],
        deterministic=stats["deterministic"],
        errors=stats["errors"],
        tokens_total=stats["tokens"]["total"],
        dry_run=dry_run,
    )

    # Onda P2: consolidação de métricas agregadas em daily_metrics_corretora.
    # Consolidação falhar NÃO pode quebrar o trabalho principal (lead_analyses).
    if not dry_run:
        try:
            from app.services.consolidacao_metricas import consolidar_todas, gravar_snapshot
            metricas = consolidar_todas()
            n = gravar_snapshot(metricas)
            log.info("pipeline_consolidacao_ok", corretoras=n)
            stats["consolidacao"] = {"corretoras": n}
        except Exception:
            log.exception("pipeline_consolidacao_failed")
            stats["consolidacao"] = {"error": True}

    # Onda P2.2: métricas do funil via events Kommo (passagens por etapa).
    # Falha NÃO quebra o Inspector — bloco isolado, additive.
    if not dry_run:
        try:
            from datetime import datetime, timezone
            from zoneinfo import ZoneInfo
            from app.services.funil_metricas import consolidar_funil_metricas

            SP_TZ = ZoneInfo("America/Sao_Paulo")
            now_sp = datetime.now(SP_TZ)
            primeiro_dia = now_sp.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            from_unix = int(primeiro_dia.astimezone(timezone.utc).timestamp())
            to_unix = int(now_sp.astimezone(timezone.utc).timestamp())

            funil_stats = consolidar_funil_metricas(from_unix, to_unix)
            log.info("pipeline_funil_ok", **funil_stats)
            stats["funil"] = funil_stats
        except Exception:
            log.exception("pipeline_funil_failed")
            stats["funil"] = {"error": True}

    # Onda 7.5: snapshots_diarios + snapshots_corretora — fonte única de verdade.
    # Inspector grava, Assistente lê. Falha NÃO quebra o pipeline principal.
    #
    # IMPORTANTE: a run matinal (~07h SP) atualiza o snapshot do DIA ANTERIOR,
    # não do dia em curso. Snapshot do dia atual só é gravado pela run noturna
    # (>=18h SP), quando o time já fechou. Caso contrário, o briefing das 20h
    # poderia ler números congelados das 7h se a run noturna falhasse (bug
    # observado em 2026-05-18: snapshot Pessoa5 3/1, real 161/68).
    if not dry_run:
        try:
            from zoneinfo import ZoneInfo as _ZI
            from datetime import timedelta as _td
            from app.inspector.calcular_metricas import calcular_metricas_dia

            _sp = _ZI("America/Sao_Paulo")
            _agora_sp = datetime.now(_sp)
            _data_snap = _agora_sp.date() if _agora_sp.hour >= 18 else (_agora_sp.date() - _td(days=1))
            _result = calcular_metricas_dia(_data_snap)
            log.info(
                "pipeline_snapshot_ok",
                data=str(_data_snap),
                hora_sp=_agora_sp.hour,
                total_contatos=_result["diario"].get("total_contatos", 0),
                linhas_corretora=_result["linhas_corretora"],
            )
            stats["snapshot"] = {
                "diario": _result["diario"],
                "linhas_corretora": _result["linhas_corretora"],
            }
        except Exception:
            log.exception("pipeline_snapshot_failed")
            stats["snapshot"] = {"error": True}

    # Onda 7.5: snapshots_analise — grava análise qualitativa do dia.
    # Agrega alertas, quentes e mornos do stats atual. Idempotente.
    if not dry_run:
        try:
            from zoneinfo import ZoneInfo as _ZI2
            import json as _json
            _sp2 = _ZI2("America/Sao_Paulo")
            _hoje2 = datetime.now(_sp2).date()
            _quentes = [
                a for a in stats.get("analyses", [])
                if a.get("temperatura") == "quente"
            ] if stats.get("analyses") else []
            _mornos = [
                a for a in stats.get("analyses", [])
                if a.get("temperatura") == "morno" and a.get("gap_identificado")
            ] if stats.get("analyses") else []
            _alertas_snap = {
                "instancias_offline": stats.get("instancias_offline", []),
                "erros": stats.get("errors", 0),
                "skipped_sem_dado": stats.get("skipped_sem_dado", 0),
            }
            with SessionLocal() as _s:
                _s.execute(
                    text("""
                        INSERT INTO snapshots_analise
                          (data, narrativa_text, padroes_text, alertas,
                           leads_quentes, leads_mornos, gerado_em)
                        VALUES
                          (:data, :narrativa, :padroes, CAST(:alertas AS jsonb),
                           CAST(:quentes AS jsonb), CAST(:mornos AS jsonb), NOW())
                        ON CONFLICT (data) DO UPDATE SET
                          narrativa_text = EXCLUDED.narrativa_text,
                          padroes_text   = EXCLUDED.padroes_text,
                          alertas        = EXCLUDED.alertas,
                          leads_quentes  = EXCLUDED.leads_quentes,
                          leads_mornos   = EXCLUDED.leads_mornos,
                          gerado_em      = NOW()
                    """),
                    {
                        "data": _hoje2,
                        "narrativa": None,  # gerado pelo Agente Dona
                        "padroes": None,    # gerado pelo Agente Dona
                        "alertas": _json.dumps(_alertas_snap),
                        "quentes": _json.dumps(_quentes),
                        "mornos": _json.dumps(_mornos),
                    },
                )
                _s.commit()
            log.info(
                "pipeline_snapshot_analise_ok",
                data=str(_hoje2),
                n_quentes=len(_quentes),
                n_mornos=len(_mornos),
            )
            stats["snapshot_analise"] = {
                "data": str(_hoje2),
                "n_quentes": len(_quentes),
                "n_mornos": len(_mornos),
            }
        except Exception:
            log.exception("pipeline_snapshot_analise_failed")
            stats["snapshot_analise"] = {"error": True}


def _esfriou_recentemente(corretora_id: int, lead_id: str) -> bool:
    """
    True se nos últimos 7 dias houve análise dessa lead com temperatura='morno'
    (anterior à mais recente). Indica morno→frio.
    """
    from sqlalchemy import text as _sql

    with SessionLocal() as s:
        row = s.execute(
            _sql(
                "SELECT 1 FROM lead_analyses "
                "WHERE lead_id = :lid AND corretora_id = :cid "
                "  AND temperatura = 'morno' "
                "  AND analyzed_at >= NOW() - INTERVAL '7 days' "
                "LIMIT 1"
            ),
            {"lid": str(lead_id), "cid": corretora_id},
        ).first()
    return bool(row)


def _gerar_coaching_corretora(
    corretora_id: int,
    corretora_nome: str,
    coaching_pool: list[dict[str, Any]],
    max_quentes: int = 3,
    max_esfriaram: int = 2,
) -> int:
    """
    Seleciona até 3 leads quentes + até 2 leads que esfriaram (morno→frio
    em 7 dias) e gera 1 observação de coaching pra cada. Grava em
    coaching_observations. Retorna nº de observações gravadas.
    """
    if not coaching_pool:
        return 0

    quentes = [p for p in coaching_pool if p["temperatura"] == "quente"][:max_quentes]
    frios = [p for p in coaching_pool if p["temperatura"] == "frio"]
    esfriaram = [
        p for p in frios
        if _esfriou_recentemente(corretora_id, p["lead_id"])
    ][:max_esfriaram]

    n = 0
    for p in quentes:
        try:
            obs = analisar_coaching(
                corretora_nome=corretora_nome,
                corretora_id=corretora_id,
                lead_nome=p["lead_nome"],
                lead_id=_safe_int(p["lead_id"]),
                conversa=p["messages"],
                origem="lead_quente",
            )
            if obs:
                gravar_observacao(obs)
                n += 1
        except Exception:
            log.exception("coaching_quente_error",
                          corretora_id=corretora_id, lead_id=p["lead_id"])

    for p in esfriaram:
        try:
            obs = analisar_coaching(
                corretora_nome=corretora_nome,
                corretora_id=corretora_id,
                lead_nome=p["lead_nome"],
                lead_id=_safe_int(p["lead_id"]),
                conversa=p["messages"],
                origem="lead_esfriou",
            )
            if obs:
                gravar_observacao(obs)
                n += 1
        except Exception:
            log.exception("coaching_esfriou_error",
                          corretora_id=corretora_id, lead_id=p["lead_id"])

    log.info("coaching_corretora_done",
             corretora_id=corretora_id, gravadas=n,
             quentes=len(quentes), esfriaram=len(esfriaram))
    return n


def _safe_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _finalize(output: LeadAnalysisOutput, stats: dict[str, Any], dry_run: bool) -> None:
    """Atualiza estatísticas e grava (se não dry_run)."""
    stats["processed"] += 1

    status = output.status
    if status in stats["by_status"]:
        stats["by_status"][status] += 1

    temp = output.temperatura
    if temp in stats["by_temperatura"]:
        stats["by_temperatura"][temp] += 1

    mode = output.analysis_mode
    if mode in stats["by_mode"]:
        stats["by_mode"][mode] += 1

    # Tracking por corretora
    nome = output.corretora
    if nome not in stats.setdefault("by_corretora", {}):
        stats["by_corretora"][nome] = {
            "total": 0, "llm_calls": 0, "deep_analyzed": 0,
            "by_mode": {"conversa_whatsapp": 0, "tarefa_atrasada": 0,
                        "sem_atividade": 0, "sem_atividade_hoje": 0, "sem_dado_historico": 0},
            "by_temperatura": {"frio": 0, "morno": 0, "quente": 0},
            "tokens_classifier": 0, "tokens_analyzer": 0,
        }
    c = stats["by_corretora"][nome]
    c["total"] += 1
    if mode in c["by_mode"]:
        c["by_mode"][mode] += 1
    if temp in c["by_temperatura"]:
        c["by_temperatura"][temp] += 1
    if output.tokens_classifier:
        c["tokens_classifier"] += output.tokens_classifier
        c["llm_calls"] += 1
    if output.tokens_analyzer:
        c["tokens_analyzer"] += output.tokens_analyzer
        c["deep_analyzed"] += 1

    # Sempre acumula em analyses (usado por snapshots_analise e dry_run debug)
    stats["analyses"].append(output.model_dump())
    if not dry_run:
        _save_analysis(output)
