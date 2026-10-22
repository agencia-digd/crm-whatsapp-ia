"""Persistência de mensagens de conversa Assistente ↔ corretoras/donas."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, select

from app.db.models import Conversation
from app.db.session import SessionLocal


def append_message(
    phone: str,
    sender_kind: str,
    sender_id: int | None,
    message_role: str,
    text: str,
    message_id: str | None = None,
) -> int:
    """Insere mensagem. Retorna id. Lança IntegrityError se message_id duplicado."""
    with SessionLocal() as s:
        row = Conversation(
            phone=phone,
            sender_kind=sender_kind,
            sender_id=sender_id,
            message_role=message_role,
            text=text,
            message_id=message_id,
        )
        s.add(row)
        s.commit()
        return row.id


def load_history(
    phone: str,
    max_messages: int = 20,
    max_age_hours: int = 6,
) -> list[dict[str, Any]]:
    """
    Últimas N mensagens OU últimas M horas — o que vier PRIMEIRO (menor janela).

    Retorna em ordem cronológica (antigo → novo) pra montar prompt.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    with SessionLocal() as s:
        rows = s.execute(
            select(Conversation)
            .where(Conversation.phone == phone)
            .where(Conversation.created_at >= cutoff)
            .order_by(desc(Conversation.created_at))
            .limit(max_messages)
        ).scalars().all()

    rows = list(reversed(rows))  # cronológico pra prompt
    return [
        {
            "role": r.message_role,
            "sender_kind": r.sender_kind,
            "text": r.text,
            "created_at": r.created_at,
        }
        for r in rows
    ]


def is_duplicate(message_id: str | None) -> bool:
    """True se message_id já existe na tabela."""
    if not message_id:
        return False
    with SessionLocal() as s:
        existing = s.execute(
            select(Conversation.id).where(Conversation.message_id == message_id).limit(1)
        ).scalar()
    return existing is not None
