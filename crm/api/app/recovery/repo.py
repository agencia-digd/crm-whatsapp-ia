"""
Camada de DB do módulo Recovery.

Toda interação com `recovery_conversations` e `recovery_messages` passa daqui.
Mantém o orchestrator livre de SQL.
"""
from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import RecoveryConversation, RecoveryMessage
from app.recovery.conversation import Conversation, State
from app.recovery.responder import Message

log = structlog.get_logger()


def get_or_create(
    session: Session,
    *,
    lead_id: str,
    lead_phone: str,
    lead_name: str | None,
) -> RecoveryConversation:
    """Busca conversa do lead ou cria nova em estado AGUARDANDO."""
    row = session.scalar(
        select(RecoveryConversation).where(RecoveryConversation.lead_id == lead_id)
    )
    if row is not None:
        return row

    row = RecoveryConversation(
        lead_id=lead_id,
        lead_phone=lead_phone,
        lead_name=lead_name,
        state=State.AGUARDANDO.value,
    )
    session.add(row)
    session.flush()
    log.info("recovery_conversation_created", lead_id=lead_id, phone_last4=lead_phone[-4:])
    return row


def find_by_phone(session: Session, lead_phone: str) -> RecoveryConversation | None:
    return session.scalar(
        select(RecoveryConversation).where(RecoveryConversation.lead_phone == lead_phone)
    )


def claim_new(
    session: Session, *, lead_id: str, lead_phone: str, lead_name: str | None
) -> bool:
    """Reserva atômica da conversa do lead (claim ANTES de enviar o HSM).

    INSERT ... ON CONFLICT (lead_id) DO NOTHING. Retorna True se ESTE processo
    criou a linha (ganhou o claim → deve enviar) e False se já existia (outra
    wave/execução concorrente já está tratando esse lead → pular). Fecha a race
    do check-then-send: duas waves simultâneas nunca despacham 2 HSMs pro mesmo
    lead (Codex #4). `lead_id` tem unique constraint.
    """
    stmt = (
        pg_insert(RecoveryConversation)
        .values(lead_id=lead_id, lead_phone=lead_phone, lead_name=lead_name)
        .on_conflict_do_nothing(index_elements=["lead_id"])
        .returning(RecoveryConversation.id)
    )
    claimed = session.execute(stmt).first() is not None
    session.commit()
    return claimed


def to_domain(row: RecoveryConversation) -> Conversation:
    """Converte ORM → dataclass pura pra rodar state machine."""
    return Conversation(
        lead_id=row.lead_id,
        state=State(row.state),
        turns=row.turns,
        last_lead_msg_at=row.last_lead_msg_at,
        last_corretor_msg_at=row.last_corretor_msg_at,
        reengage_done=row.reengage_done,
        end_reason=row.end_reason,  # type: ignore[arg-type]
        qualified_data=row.qualified_data or {},
    )


def save_state(session: Session, row: RecoveryConversation, conv: Conversation) -> None:
    """Persiste o estado da dataclass de volta no ORM."""
    row.state = conv.state.value
    row.turns = conv.turns
    row.last_lead_msg_at = conv.last_lead_msg_at
    row.last_corretor_msg_at = conv.last_corretor_msg_at
    row.reengage_done = conv.reengage_done
    row.end_reason = conv.end_reason
    row.qualified_data = conv.qualified_data or None
    row.updated_at = datetime.now(timezone.utc)
    session.flush()


def append_message(
    session: Session, lead_id: str, role: str, text: str
) -> RecoveryMessage:
    msg = RecoveryMessage(lead_id=lead_id, role=role, text_content=text)
    session.add(msg)
    session.flush()
    return msg


def load_history(session: Session, lead_id: str, limit: int = 50) -> list[Message]:
    """Carrega histórico cronológico (mais antigos primeiro) pro responder."""
    rows = session.scalars(
        select(RecoveryMessage)
        .where(RecoveryMessage.lead_id == lead_id)
        .order_by(RecoveryMessage.ts.asc())
        .limit(limit)
    ).all()
    return [Message(role=r.role, text=r.text_content) for r in rows]  # type: ignore[arg-type]
