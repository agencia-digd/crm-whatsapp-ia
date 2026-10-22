"""API REST do Prospector — alimenta o dash em app.example.com/site-hunter.

Endpoints:
  GET  /api/hunter/leads                   (filtros: status, segmento, cidade)
  GET  /api/hunter/leads/{slug}            detalhe + histórico
  POST /api/hunter/leads/{slug}/approve    aprova + dispara WhatsApp (background)
  POST /api/hunter/leads/{slug}/reject     marca rejected
  POST /api/hunter/leads/{slug}/notes      adiciona nota
  GET  /api/hunter/kpis                    contagens por status

Auth: protegido por Basic Auth no nginx (mesma do painel Acme).
"""
from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Body, Header, HTTPException, Query
from sqlalchemy import desc, func, select

from app.config import settings
from app.db.models import HunterMessage, HunterProspect
from app.db.session import SessionLocal
from app.integrations.evolution_client import EvolutionClient
from app.services import hunter_repo

log = structlog.get_logger()

router = APIRouter(prefix="/api/hunter", tags=["hunter"])

SITEHUNTER_REPO = Path("/opt/crm/projetos/site-hunter")
STATE_FILE = SITEHUNTER_REPO / "dashboard/state.json"


def _prospect_to_dict(p: HunterProspect) -> dict[str, Any]:
    return {
        "id": p.id,
        "phone": p.phone,
        "nome": p.nome,
        "segmento": p.segmento,
        "cidade": p.cidade,
        "source": p.source,
        "slug": p.slug,
        "preview_url": p.preview_url,
        "state": p.state,
        "turns": p.turns,
        "end_reason": p.end_reason,
        "qualified_data": p.qualified_data,
        "hsm_sent_at": p.hsm_sent_at.isoformat() if p.hsm_sent_at else None,
        "last_lead_msg_at": p.last_lead_msg_at.isoformat() if p.last_lead_msg_at else None,
        "last_assistente_msg_at": p.last_assistente_msg_at.isoformat() if p.last_assistente_msg_at else None,
        "notes": p.notes,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


@router.get("/kpis")
def kpis(painel_user: str = Header(default="", alias="X-Painel-User")) -> dict[str, Any]:
    with SessionLocal() as s:
        rows = s.execute(
            select(HunterProspect.state, func.count(HunterProspect.id))
            .group_by(HunterProspect.state)
        ).all()
    by_state = {r[0]: r[1] for r in rows}
    return {
        "total": sum(by_state.values()),
        "pending": by_state.get("pending", 0),
        "sent": by_state.get("sent", 0),
        "responded": by_state.get("responded", 0),
        "qualificando": by_state.get("qualificando", 0),
        "qualificado": by_state.get("qualificado", 0),
        "perdido": by_state.get("perdido", 0),
        "opted_out": by_state.get("opted_out", 0),
        "rejected": by_state.get("rejected", 0),
        "by_state": by_state,
        "user": painel_user,
    }


@router.get("/leads")
def list_leads(
    status: str | None = Query(default=None),
    segmento: str | None = Query(default=None),
    cidade: str | None = Query(default=None),
    limit: int = Query(default=200, le=500),
    painel_user: str = Header(default="", alias="X-Painel-User"),
) -> dict[str, Any]:
    with SessionLocal() as s:
        q = select(HunterProspect).order_by(desc(HunterProspect.created_at))
        if status:
            q = q.where(HunterProspect.state == status)
        if segmento:
            q = q.where(HunterProspect.segmento == segmento)
        if cidade:
            q = q.where(HunterProspect.cidade == cidade)
        q = q.limit(limit)
        rows = s.execute(q).scalars().all()
    return {"leads": [_prospect_to_dict(p) for p in rows]}


@router.get("/leads/{slug}")
def get_lead(slug: str,
             painel_user: str = Header(default="", alias="X-Painel-User")
             ) -> dict[str, Any]:
    with SessionLocal() as s:
        p = s.execute(
            select(HunterProspect).where(HunterProspect.slug == slug)
        ).scalar_one_or_none()
        if not p:
            raise HTTPException(status_code=404, detail="lead não encontrado")
        history = s.execute(
            select(HunterMessage)
            .where(HunterMessage.phone == p.phone)
            .order_by(HunterMessage.ts.asc())
        ).scalars().all()
    return {
        "lead": _prospect_to_dict(p),
        "history": [
            {"role": m.role, "text": m.text_content, "ts": m.ts.isoformat()}
            for m in history
        ],
    }


def _render_message(p: HunterProspect) -> str:
    nome = p.nome or "seu negócio"
    cidade = p.cidade or "Cidade"
    return (
        f"Oi! 🐱 Aqui é a Assistente, da agencia — agência de marketing aqui de {cidade}.\n\n"
        f"Vi o {nome} no Google e achei o trabalho de vocês muito bonito, "
        f"mas notei que vocês ainda não têm um site próprio.\n\n"
        f"Fiz um exemplo rapidinho usando as fotos e o estilo de vocês pra mostrar "
        f"como ficaria — dá uma olhada quando puder:\n\n"
        f"{p.preview_url}\n\n"
        f"Se curtirem, a gente conversa sobre publicar com domínio próprio "
        f"e integrar com agendamento, Instagram e WhatsApp. 😉\n\n"
        f"Tô por aqui pra qualquer dúvida!"
    )


def _send_via_evolution(slug: str) -> None:
    """Job background: dispara WhatsApp e atualiza estado."""
    log.info("hunter_dispatch_start", slug=slug)
    with SessionLocal() as s:
        p = s.execute(
            select(HunterProspect).where(HunterProspect.slug == slug)
        ).scalar_one_or_none()
    if not p:
        log.warning("hunter_dispatch_lead_gone", slug=slug)
        return
    if p.state in ("sent", "qualificando", "qualificado", "responded", "opted_out", "perdido"):
        log.info("hunter_dispatch_skip_state", slug=slug, state=p.state)
        return
    msg = _render_message(p)
    try:
        EvolutionClient(
            base_url=settings.evolution_base_url,
            api_key=settings.evolution_api_key,
        ).send_text(settings.evolution_assistente_instance, p.phone, msg)
        hunter_repo.update_state(
            p.phone, "sent", hsm_sent_at=datetime.now(timezone.utc),
        )
        hunter_repo.append_message(p.phone, "assistente", msg)
        log.info("hunter_dispatch_ok", slug=slug, phone_last4=p.phone[-4:])
    except Exception as e:
        log.error("hunter_dispatch_fail", slug=slug, error=str(e))


@router.post("/leads/{slug}/approve")
def approve_lead(
    slug: str,
    background: BackgroundTasks,
    painel_user: str = Header(default="", alias="X-Painel-User"),
) -> dict[str, Any]:
    with SessionLocal() as s:
        p = s.execute(
            select(HunterProspect).where(HunterProspect.slug == slug)
        ).scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="lead não encontrado")
    if p.state in ("sent", "qualificando", "qualificado", "responded"):
        return {"status": "already_processed", "state": p.state}
    if p.state == "opted_out":
        raise HTTPException(status_code=400, detail="lead opted_out — não disparar")

    background.add_task(_send_via_evolution, slug)
    log.info("hunter_approve_queued", slug=slug, by=painel_user or "?")
    return {"status": "queued", "slug": slug, "by": painel_user}


@router.post("/leads/{slug}/reject")
def reject_lead(
    slug: str,
    motivo: str = Query(default="rejected"),
    painel_user: str = Header(default="", alias="X-Painel-User"),
) -> dict[str, Any]:
    with SessionLocal() as s:
        p = s.execute(
            select(HunterProspect).where(HunterProspect.slug == slug)
        ).scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="lead não encontrado")
    hunter_repo.update_state(p.phone, "rejected", end_reason=motivo)
    log.info("hunter_rejected", slug=slug, motivo=motivo, by=painel_user)
    return {"status": "ok", "slug": slug, "state": "rejected"}


@router.post("/leads/{slug}/notes")
def add_note(
    slug: str,
    text: str = Body(..., embed=True),
    painel_user: str = Header(default="", alias="X-Painel-User"),
) -> dict[str, Any]:
    with SessionLocal() as s:
        p = s.execute(
            select(HunterProspect).where(HunterProspect.slug == slug)
        ).scalar_one_or_none()
        if not p:
            raise HTTPException(status_code=404, detail="lead não encontrado")
        old = (p.notes or "").strip()
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        prefix = f"[{ts} · {painel_user or '?'}]"
        p.notes = f"{old}\n{prefix} {text}".strip() if old else f"{prefix} {text}"
        s.commit()
    return {"status": "ok", "slug": slug}


@router.get("/segmentos")
def list_segmentos(
    painel_user: str = Header(default="", alias="X-Painel-User"),
) -> dict[str, Any]:
    with SessionLocal() as s:
        rows = s.execute(
            select(HunterProspect.segmento, func.count(HunterProspect.id))
            .group_by(HunterProspect.segmento)
            .order_by(desc(func.count(HunterProspect.id)))
        ).all()
    return {"segmentos": [{"name": r[0], "count": r[1]} for r in rows if r[0]]}
