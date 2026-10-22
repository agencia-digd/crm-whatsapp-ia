"""Repositório do Hunter — interface limpa sobre hunter_prospects + hunter_messages.

Trocar o backend (Sheets, S3, qualquer outro) só envolve mexer neste arquivo.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import select, text, update

from app.db.models import HunterMessage, HunterProspect
from app.db.session import SessionLocal

log = structlog.get_logger()


def _digits(raw: str) -> str:
    d = re.sub(r"\D", "", raw or "")
    if d and not d.startswith("55"):
        d = "55" + d
    return d


def get_by_phone(phone: str) -> HunterProspect | None:
    p = _digits(phone)
    if not p:
        return None
    with SessionLocal() as s:
        return s.execute(
            select(HunterProspect).where(HunterProspect.phone == p)
        ).scalar_one_or_none()


def upsert(
    *,
    phone: str,
    nome: str | None,
    segmento: str | None = None,
    cidade: str | None = None,
    slug: str | None = None,
    preview_url: str | None = None,
    state: str = "pending",
    source: str = "site-hunter",
) -> HunterProspect:
    p = _digits(phone)
    with SessionLocal() as s:
        existing = s.execute(
            select(HunterProspect).where(HunterProspect.phone == p)
        ).scalar_one_or_none()
        if existing:
            return existing
        prospect = HunterProspect(
            phone=p, nome=nome, segmento=segmento, cidade=cidade,
            slug=slug, preview_url=preview_url, state=state, source=source,
        )
        s.add(prospect)
        s.commit()
        s.refresh(prospect)
        return prospect


def update_state(phone: str, state: str, **fields: Any) -> bool:
    p = _digits(phone)
    with SessionLocal() as s:
        result = s.execute(
            update(HunterProspect)
            .where(HunterProspect.phone == p)
            .values(state=state, updated_at=datetime.now(timezone.utc), **fields)
        )
        s.commit()
        return result.rowcount > 0


def mark_sent(phone: str) -> bool:
    return update_state(
        phone, "sent",
        hsm_sent_at=datetime.now(timezone.utc),
    )


def mark_responded(phone: str) -> bool:
    p = _digits(phone)
    with SessionLocal() as s:
        existing = s.execute(
            select(HunterProspect).where(HunterProspect.phone == p)
        ).scalar_one_or_none()
        if not existing:
            return False
        # Não rebaixa estado se já foi qualificado/perdido/opted_out
        if existing.state in ("qualificado", "perdido", "opted_out"):
            existing.last_lead_msg_at = datetime.now(timezone.utc)
            s.commit()
            return True
        existing.state = "qualificando" if existing.turns > 0 else "responded"
        existing.last_lead_msg_at = datetime.now(timezone.utc)
        s.commit()
        return True


def mark_qualified(phone: str, data: dict[str, Any]) -> bool:
    return update_state(
        phone, "qualificado",
        qualified_data=data,
    )


def mark_opted_out(phone: str, reason: str = "opt_out") -> bool:
    return update_state(phone, "opted_out", end_reason=reason)


def mark_perdido(phone: str, reason: str) -> bool:
    return update_state(phone, "perdido", end_reason=reason)


def append_message(phone: str, role: str, text_content: str,
                   message_id: str | None = None) -> None:
    p = _digits(phone)
    if not p or not text_content:
        return
    with SessionLocal() as s:
        s.add(HunterMessage(
            phone=p, role=role, text_content=text_content, message_id=message_id,
        ))
        if role == "assistente":
            s.execute(text("""
                UPDATE hunter_prospects
                SET last_assistente_msg_at = now(),
                    turns = turns + 1,
                    updated_at = now()
                WHERE phone = :p
            """), {"p": p})
        s.commit()


def load_history(phone: str, limit: int = 40) -> list[dict[str, str]]:
    p = _digits(phone)
    with SessionLocal() as s:
        rows = s.execute(
            select(HunterMessage)
            .where(HunterMessage.phone == p)
            .order_by(HunterMessage.ts.asc())
            .limit(limit)
        ).scalars().all()
    return [{"role": r.role, "text": r.text_content,
             "ts": r.ts.isoformat()} for r in rows]


def list_pending(limit: int = 50) -> list[HunterProspect]:
    with SessionLocal() as s:
        return list(s.execute(
            select(HunterProspect)
            .where(HunterProspect.state == "pending")
            .order_by(HunterProspect.created_at.asc())
            .limit(limit)
        ).scalars().all())


def count_sent_today() -> int:
    with SessionLocal() as s:
        row = s.execute(text("""
            SELECT COUNT(*) FROM hunter_prospects
            WHERE hsm_sent_at::date = (now() AT TIME ZONE 'America/Sao_Paulo')::date
        """)).scalar_one()
        return int(row or 0)
