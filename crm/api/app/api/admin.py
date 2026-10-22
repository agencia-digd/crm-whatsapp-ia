"""Endpoints administrativos — Onda P2 / P2.2."""
from __future__ import annotations

from datetime import date as date_type, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.config import settings
from app.db.models import Corretora
from app.db.session import SessionLocal
from app.services.consolidacao_metricas import (
    consolidar_todas,
    gravar_snapshot,
)
from app.services.funil_metricas import consolidar_funil_metricas

SP_TZ = ZoneInfo("America/Sao_Paulo")

log = structlog.get_logger()

router = APIRouter(prefix="/admin", tags=["admin"])


class ConsolidarIn(BaseModel):
    data: str | None = Field(default=None, description="YYYY-MM-DD; default = hoje SP")


def _check_token(x_internal_token: str | None) -> None:
    expected = settings.copiloto_internal_token
    if not expected or x_internal_token != expected:
        raise HTTPException(status_code=401, detail="invalid token")


@router.post("/consolidar")
def admin_consolidar(
    payload: ConsolidarIn | None = None,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> dict[str, Any]:
    _check_token(x_internal_token)

    target_date: date_type | None = None
    if payload and payload.data:
        try:
            target_date = datetime.strptime(payload.data, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid_date")

    metricas = consolidar_todas()
    n = gravar_snapshot(metricas, data=target_date)

    fontes_completas_global = sorted({
        f for m in metricas.values() for f in m.fontes_completas
    })
    fontes_falhas_global = sorted({
        f for m in metricas.values() for f in m.fontes_falhas
    })

    # Nome das corretoras pra detalhamento
    with SessionLocal() as s:
        nomes_by_id = {
            r.id: r.nome
            for r in s.execute(select(Corretora)).scalars().all()
        }

    detalhes = [
        {
            "id": cid,
            "nome": nomes_by_id.get(cid, "?"),
            "metricas": {
                "total_leads_mes": m.total_leads_mes,
                "atendidos_mes": m.atendidos_mes,
                "tarefas_atrasadas": m.tarefas_atrasadas,
            },
            "fontes_completas": m.fontes_completas,
            "fontes_falhas": m.fontes_falhas,
        }
        for cid, m in sorted(metricas.items())
    ]

    # Onda P2.2 — métricas do funil via events Kommo
    funil_result: dict[str, Any] = {}
    try:
        now_sp = datetime.now(SP_TZ)
        primeiro_dia = now_sp.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        from_unix = int(primeiro_dia.astimezone(timezone.utc).timestamp())
        to_unix = int(now_sp.astimezone(timezone.utc).timestamp())
        funil_result = consolidar_funil_metricas(from_unix, to_unix)
    except Exception as exc:
        log.exception("admin_consolidar_funil_failed")
        funil_result = {"error": str(exc)}

    return {
        "status": "ok",
        "data": (target_date or datetime.now().date()).isoformat(),
        "corretoras_processadas": n,
        "fontes_completas_global": fontes_completas_global,
        "fontes_falhas_global": fontes_falhas_global,
        "detalhes_por_corretora": detalhes,
        "funil": funil_result,
    }
