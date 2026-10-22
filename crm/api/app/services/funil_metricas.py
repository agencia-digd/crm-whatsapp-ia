"""
Métricas do funil Kommo — Onda P2.2 (modelo híbrido Coorte + Passagens).

Lógica:
  - entrou_mes / em_atendimento_mes → COORTE:
      leads criados no mês, atribuídos ao responsible_user_id atual.
      em_atendimento_mes = subset que teve evento stage_to=em_atendimento.

  - agendados_mes / visitas_realizadas_mes / vendas_kommo_mes → PASSAGENS:
      qualquer lead que entrou na etapa, atribuído ao created_by do evento.
      created_by=0 excluído (automação). Distinto por (user, stage, lead).

  - vgv_kommo_mes → lead.price dos leads ganhos nas passagens.

Zero risco de regressão no Inspector (bloco isolado, additive).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import text as sql_text

from app.db.session import SessionLocal
from app.integrations.kommo_client import (
    KOMMO_STAGE_EM_ATENDIMENTO,
    KOMMO_STAGE_VENDA_GANHA,
    KOMMO_STAGE_VISITA_AGENDADA,
    KOMMO_STAGE_VISITA_REALIZADA,
    KommoClient,
)

log = structlog.get_logger()

_PASSAGENS_STAGES = frozenset({
    KOMMO_STAGE_VISITA_AGENDADA,
    KOMMO_STAGE_VISITA_REALIZADA,
    KOMMO_STAGE_VENDA_GANHA,
})

_PASSAGEM_STAGE_TO_FIELD = {
    KOMMO_STAGE_VISITA_AGENDADA: "agendados_mes",
    KOMMO_STAGE_VISITA_REALIZADA: "visitas_realizadas_mes",
    KOMMO_STAGE_VENDA_GANHA: "vendas_kommo_mes",
}


@dataclass
class FunilMetricas:
    entrou_mes: int = 0
    em_atendimento_mes: int = 0
    agendados_mes: int = 0
    visitas_realizadas_mes: int = 0
    vendas_kommo_mes: int = 0
    vgv_kommo_mes: Decimal = field(default_factory=Decimal)


# ── DB helpers ─────────────────────────────────────────────────────────────────


def _load_corretora_id_by_kommo_user_id() -> dict[int, int]:
    """Retorna {kommo_user_id: corretora_id} para corretoras ativas."""
    with SessionLocal() as s:
        rows = s.execute(sql_text(
            "SELECT kommo_user_id, id FROM corretoras "
            "WHERE ativo = true AND kommo_user_id IS NOT NULL"
        )).fetchall()
    return {int(r[0]): int(r[1]) for r in rows}


# ── Coorte: leads criados no mês ──────────────────────────────────────────────


def calcular_coorte_aquisicao(
    kommo_client: KommoClient,
    pipeline_id: int,
    from_unix: int,
    to_unix: int,
    events: list[dict[str, Any]],
) -> dict[int, dict[str, int]]:
    """
    Coorte de leads criados no intervalo, agrupados por responsible_user_id.

    entrou_mes  = total de leads criados, por corretora.
    em_atendimento_mes = subset que teve stage_to=em_atendimento nos events.

    Returns:
        {kommo_user_id: {entrou_mes, em_atendimento_mes}}
    """
    filters: dict[str, Any] = {
        "filter[created_at][from]": from_unix,
        "filter[created_at][to]": to_unix,
        "filter[pipeline_id]": pipeline_id,
    }
    coorte: dict[int, int] = {}  # {lead_id: responsible_user_id}
    for lead in kommo_client.get_leads(filters=filters, with_contacts=False):
        lid = int(lead.get("id") or 0)
        resp = int(lead.get("responsible_user_id") or 0)
        if lid and resp:
            coorte[lid] = resp

    result: dict[int, dict[str, int]] = {}
    for resp in coorte.values():
        if resp not in result:
            result[resp] = {"entrou_mes": 0, "em_atendimento_mes": 0}
        result[resp]["entrou_mes"] += 1

    # em_atendimento: distinct leads da coorte que tiveram o evento
    seen_em_at: set[tuple[int, int]] = set()
    for ev in events:
        if ev.get("stage_to") != KOMMO_STAGE_EM_ATENDIMENTO:
            continue
        lid = ev.get("lead_id", 0)
        if lid not in coorte:
            continue
        resp = coorte[lid]
        key = (resp, lid)
        if key in seen_em_at:
            continue
        seen_em_at.add(key)
        if resp in result:
            result[resp]["em_atendimento_mes"] += 1

    log.info("funil_coorte_ok", leads_mes=len(coorte), corretoras=len(result))
    return result


# ── Passagens: etapas de atividade, atribuídas ao created_by ─────────────────


def calcular_passagens_atividade(
    events: list[dict[str, Any]],
) -> dict[int, dict[str, int]]:
    """
    Passagens em agendados / visitas_realizadas / venda_ganha.

    Atribuição: created_by do evento (quem moveu a etapa).
    created_by=0 excluído (automações Kommo).
    Distinto por (created_by, stage_to, lead_id).

    Returns:
        {kommo_user_id: {agendados_mes, visitas_realizadas_mes, vendas_kommo_mes}}
    """
    seen: dict[tuple[int, int], set[int]] = {}
    result: dict[int, dict[str, int]] = {}

    for ev in events:
        stage_to = ev.get("stage_to", 0)
        if stage_to not in _PASSAGENS_STAGES:
            continue
        created_by = ev.get("created_by", 0)
        if not created_by:
            continue
        lid = ev.get("lead_id", 0)
        if not lid:
            continue

        key = (created_by, stage_to)
        bucket = seen.setdefault(key, set())
        if lid in bucket:
            continue
        bucket.add(lid)

        if created_by not in result:
            result[created_by] = {
                "agendados_mes": 0,
                "visitas_realizadas_mes": 0,
                "vendas_kommo_mes": 0,
            }
        result[created_by][_PASSAGEM_STAGE_TO_FIELD[stage_to]] += 1

    return result


# ── VGV ────────────────────────────────────────────────────────────────────────


def enriquecer_vgv_passagens(
    metricas: dict[int, FunilMetricas],
    events: list[dict[str, Any]],
    kommo_client: KommoClient,
) -> None:
    """
    Soma lead.price dos leads ganhos em Venda Ganha, atribuídos ao created_by.

    Modifica metricas in-place. Falha não levanta (log + continua).
    """
    won_by_user: dict[int, int] = {}  # {lead_id: created_by}
    for ev in events:
        if ev.get("stage_to") != KOMMO_STAGE_VENDA_GANHA:
            continue
        created_by = ev.get("created_by", 0)
        if not created_by:
            continue
        lid = ev.get("lead_id", 0)
        if lid:
            won_by_user[lid] = created_by

    if not won_by_user:
        return

    try:
        leads_data = kommo_client.get_leads_by_ids(list(won_by_user.keys()))
    except Exception:
        log.exception("funil_vgv_fetch_failed", lead_count=len(won_by_user))
        return

    for lead in leads_data:
        lid = int(lead.get("id") or 0)
        if lid not in won_by_user:
            continue
        price = Decimal(str(lead.get("price") or 0))
        uid = won_by_user[lid]
        if uid in metricas:
            metricas[uid].vgv_kommo_mes += price

    log.info("funil_vgv_ok", won_leads=len(won_by_user))


# ── Persistência ───────────────────────────────────────────────────────────────

_UPSERT_FUNIL = sql_text("""
    INSERT INTO daily_metrics_corretora
      (data, corretora_id, entrou_mes, em_atendimento_mes,
       agendados_mes, visitas_realizadas_mes,
       vendas_kommo_mes, vgv_kommo_mes, gerado_em)
    VALUES
      (CURRENT_DATE, :cid, :e, :ea, :ag, :vr, :vk, :vgv, NOW())
    ON CONFLICT (data, corretora_id) DO UPDATE SET
      entrou_mes              = EXCLUDED.entrou_mes,
      em_atendimento_mes      = EXCLUDED.em_atendimento_mes,
      agendados_mes           = EXCLUDED.agendados_mes,
      visitas_realizadas_mes  = EXCLUDED.visitas_realizadas_mes,
      vendas_kommo_mes        = EXCLUDED.vendas_kommo_mes,
      vgv_kommo_mes           = EXCLUDED.vgv_kommo_mes,
      gerado_em               = NOW()
""")


def gravar_funil_metricas(
    metricas: dict[int, FunilMetricas],
    corretora_id_by_kommo: dict[int, int],
) -> int:
    """
    UPSERT em daily_metrics_corretora (data=CURRENT_DATE).

    Returns:
        Nº de corretoras gravadas.
    """
    affected = 0
    with SessionLocal() as s:
        for kommo_user_id, m in metricas.items():
            cid = corretora_id_by_kommo.get(kommo_user_id)
            if cid is None:
                log.warning("funil_unknown_kommo_user", kommo_user_id=kommo_user_id)
                continue
            s.execute(_UPSERT_FUNIL, {
                "cid": cid,
                "e": m.entrou_mes,
                "ea": m.em_atendimento_mes,
                "ag": m.agendados_mes,
                "vr": m.visitas_realizadas_mes,
                "vk": m.vendas_kommo_mes,
                "vgv": float(m.vgv_kommo_mes),
            })
            affected += 1
        s.commit()

    log.info("funil_metricas_gravadas", corretoras=affected)
    return affected


# ── Ponto de entrada para Inspector e /admin/consolidar ──────────────────────


def consolidar_funil_metricas(from_unix: int, to_unix: int) -> dict[str, Any]:
    """
    Orquestra coleta → coorte → passagens → VGV → gravação.

    Cria KommoClient internamente a partir de settings.
    Retorna dict de stats pra log do Inspector.
    Lança em caso de falha total (Inspector captura em try/except).
    """
    from app.config import settings
    from app.integrations.kommo_client import KOMMO_PIPELINE_ACME

    kommo_client = KommoClient(
        subdomain=settings.kommo_subdomain,
        token=settings.kommo_token,
    )

    events = kommo_client.get_status_changed_events(
        pipeline_id=KOMMO_PIPELINE_ACME,
        from_unix=from_unix,
        to_unix=to_unix,
    )

    coorte = calcular_coorte_aquisicao(
        kommo_client=kommo_client,
        pipeline_id=KOMMO_PIPELINE_ACME,
        from_unix=from_unix,
        to_unix=to_unix,
        events=events,
    )

    passagens = calcular_passagens_atividade(events)

    all_users = set(coorte.keys()) | set(passagens.keys())
    metricas: dict[int, FunilMetricas] = {}
    for uid in all_users:
        m = FunilMetricas()
        if uid in coorte:
            m.entrou_mes = coorte[uid]["entrou_mes"]
            m.em_atendimento_mes = coorte[uid]["em_atendimento_mes"]
        if uid in passagens:
            m.agendados_mes = passagens[uid]["agendados_mes"]
            m.visitas_realizadas_mes = passagens[uid]["visitas_realizadas_mes"]
            m.vendas_kommo_mes = passagens[uid]["vendas_kommo_mes"]
        metricas[uid] = m

    enriquecer_vgv_passagens(metricas, events, kommo_client)

    corretora_id_by_kommo = _load_corretora_id_by_kommo_user_id()
    gravadas = gravar_funil_metricas(metricas, corretora_id_by_kommo)

    resumo: dict[str, Any] = {
        "events_total": len(events),
        "corretoras_gravadas": gravadas,
    }
    for uid, m in metricas.items():
        cid = corretora_id_by_kommo.get(uid, uid)
        resumo[f"corretora_{cid}"] = {
            "entrou_mes": m.entrou_mes,
            "em_atendimento_mes": m.em_atendimento_mes,
            "agendados_mes": m.agendados_mes,
            "visitas_realizadas_mes": m.visitas_realizadas_mes,
            "vendas_kommo_mes": m.vendas_kommo_mes,
            "vgv_kommo_mes": float(m.vgv_kommo_mes),
        }

    log.info("funil_consolidacao_ok", **resumo)
    return resumo
