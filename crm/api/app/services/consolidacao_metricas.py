"""
Consolidação diária de métricas agregadas em daily_metrics_corretora.

Disparado pelo Inspector ao fim do ciclo + pelo endpoint /admin/consolidar.
Cada fonte é envolvida em try/except + retry 3x. Falha em uma fonte vira
None no campo + entrada em fontes_falhas.

PRIVACIDADE: toda query Kommo passa kommo_user_id da corretora alvo.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select, text as sql_text

from app.config import settings
from app.db.models import Corretora, LeadAnalysis
from app.db.session import SessionLocal

log = structlog.get_logger()

SP_TZ = ZoneInfo("America/Sao_Paulo")

RETRY_BACKOFFS = [1.0, 3.0, 9.0]


@dataclass
class MetricasCorretora:
    corretora_id: int
    total_leads_mes: int | None = None
    atendidos_mes: int | None = None
    tarefas_atrasadas: int | None = None
    fontes_completas: list[str] = field(default_factory=list)
    fontes_falhas: list[str] = field(default_factory=list)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _start_of_month_sp() -> datetime:
    now = datetime.now(SP_TZ)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _retry(fn, fonte: str, *args, **kwargs):
    """Tenta executar `fn` com retry 3x backoff exponencial. Lança se falhar todas."""
    last_exc: Exception | None = None
    for attempt, wait_s in enumerate(RETRY_BACKOFFS, start=1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            log.warning("consolidacao_retry", fonte=fonte, attempt=attempt, error=str(e))
            if attempt < len(RETRY_BACKOFFS):
                time.sleep(wait_s)
    assert last_exc is not None
    raise last_exc


# ── Coletores por fonte (cada um pode falhar independente) ───────────────────


def _coletar_tarefas_atrasadas(kommo_user_id: int) -> int:
    """
    COUNT de tarefas atrasadas dessa corretora no pipeline Acme,
    excluindo leads em status final (Venda ganha / Venda perdida).

    Sem esse filtro, vinha inflacionado (ex: Pessoa3 89 vs verdade 38)
    porque /tasks no Kommo é cross-pipeline e cobre leads finalizados.
    """
    from app.integrations.kommo_client import KommoClient

    client = KommoClient(subdomain=settings.kommo_subdomain, token=settings.kommo_token)
    tasks = client.get_overdue_tasks_in_pipeline(
        user_id=kommo_user_id,
        pipeline_id=settings.kommo_pipeline_id_main,
        exclude_final_statuses=True,
    )
    return len(tasks)


def _coletar_total_leads_mes(kommo_user_id: int) -> int:
    """COUNT de leads atribuídos à corretora criados no mês corrente, no pipeline Acme."""
    from app.integrations.kommo_client import KommoClient

    client = KommoClient(subdomain=settings.kommo_subdomain, token=settings.kommo_token)
    inicio_mes_ts = int(_start_of_month_sp().timestamp())
    filters: dict[str, Any] = {
        "filter[pipeline_id]": settings.kommo_pipeline_id_main,
        "filter[responsible_user_id][]": kommo_user_id,
        "filter[created_at][from]": inicio_mes_ts,
    }
    n = 0
    for _ in client.get_leads(filters=filters, with_contacts=False, limit=250):
        n += 1
    return n


def _coletar_atendidos_mes(corretora_id: int) -> int:
    """
    Aproximação via lead_analyses: leads distintos analisados no mês com status='atendido'.

    Limitação documentada: cobre só leads que passaram pelo Inspector neste mês.
    """
    inicio = _start_of_month_sp().replace(tzinfo=None)  # lead_analyses.analyzed_at é naive
    with SessionLocal() as s:
        n = s.execute(
            sql_text(
                "SELECT COUNT(DISTINCT lead_id) FROM lead_analyses "
                "WHERE corretora_id = :cid "
                "AND analyzed_at >= :inicio "
                "AND status = 'atendido'"
            ),
            {"cid": corretora_id, "inicio": inicio},
        ).scalar()
    return int(n or 0)


# ── Consolidador por corretora ───────────────────────────────────────────────


def consolidar_corretora(corretora_id: int) -> MetricasCorretora:
    """Lê 3 fontes, marca falhas, retorna estrutura. Não levanta — encapsula erros."""
    with SessionLocal() as s:
        c = s.execute(
            select(Corretora).where(Corretora.id == corretora_id)
        ).scalar_one_or_none()
    if not c:
        raise ValueError(f"corretora_id {corretora_id} não encontrada")
    if not c.kommo_user_id:
        raise ValueError(f"corretora '{c.nome}' sem kommo_user_id")

    m = MetricasCorretora(corretora_id=corretora_id)

    # tarefas_atrasadas (Kommo)
    try:
        m.tarefas_atrasadas = _retry(_coletar_tarefas_atrasadas, "kommo_tasks", c.kommo_user_id)
        m.fontes_completas.append("kommo_tasks")
    except Exception as e:
        log.error("consolidacao_kommo_tasks_failed", corretora_id=corretora_id, error=str(e))
        m.fontes_falhas.append("kommo_tasks")

    # total_leads_mes (Kommo)
    try:
        m.total_leads_mes = _retry(_coletar_total_leads_mes, "kommo_leads", c.kommo_user_id)
        m.fontes_completas.append("kommo_leads")
    except Exception as e:
        log.error("consolidacao_kommo_leads_failed", corretora_id=corretora_id, error=str(e))
        m.fontes_falhas.append("kommo_leads")

    # atendidos_mes (lead_analyses; aproximação)
    try:
        m.atendidos_mes = _retry(_coletar_atendidos_mes, "lead_analyses", corretora_id)
        m.fontes_completas.append("lead_analyses")
    except Exception as e:
        log.error("consolidacao_lead_analyses_failed", corretora_id=corretora_id, error=str(e))
        m.fontes_falhas.append("lead_analyses")

    return m


def consolidar_todas() -> dict[int, MetricasCorretora]:
    """Loop pelas corretoras ativas. Falha em uma não para outras."""
    with SessionLocal() as s:
        ids_nomes = [
            (r.id, r.nome) for r in s.execute(
                select(Corretora).where(Corretora.ativo.is_(True)).order_by(Corretora.id)
            ).scalars().all()
        ]

    out: dict[int, MetricasCorretora] = {}
    for cid, nome in ids_nomes:
        try:
            out[cid] = consolidar_corretora(cid)
        except Exception as e:
            log.error("consolidacao_corretora_failed", corretora_id=cid, nome=nome, error=str(e))
            out[cid] = MetricasCorretora(
                corretora_id=cid,
                fontes_falhas=["consolidar_corretora_exception"],
            )
    return out


# ── Persistência ─────────────────────────────────────────────────────────────


UPSERT_SQL = sql_text(
    """
    INSERT INTO daily_metrics_corretora
      (data, corretora_id, total_leads_mes, atendidos_mes,
       tarefas_atrasadas, fontes_completas, fontes_falhas, gerado_em)
    VALUES
      (:data, :cid, :tlm, :am, :ta, :fc, :ff, NOW())
    ON CONFLICT (data, corretora_id) DO UPDATE SET
      total_leads_mes  = EXCLUDED.total_leads_mes,
      atendidos_mes    = EXCLUDED.atendidos_mes,
      tarefas_atrasadas = EXCLUDED.tarefas_atrasadas,
      fontes_completas = EXCLUDED.fontes_completas,
      fontes_falhas    = EXCLUDED.fontes_falhas,
      gerado_em        = NOW()
    """
)


def gravar_snapshot(
    metricas: dict[int, MetricasCorretora],
    data: date | None = None,
) -> int:
    """UPSERT idempotente em daily_metrics_corretora. Retorna nº de linhas afetadas."""
    if data is None:
        data = datetime.now(SP_TZ).date()
    affected = 0
    with SessionLocal() as s:
        for cid, m in metricas.items():
            s.execute(UPSERT_SQL, {
                "data": data,
                "cid": cid,
                "tlm": m.total_leads_mes,
                "am": m.atendidos_mes,
                "ta": m.tarefas_atrasadas,
                "fc": m.fontes_completas,
                "ff": m.fontes_falhas,
            })
            affected += 1
        s.commit()
    return affected
