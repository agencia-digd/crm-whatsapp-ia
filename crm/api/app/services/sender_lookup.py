"""Identifica quem é o remetente (corretora ativa ou interna Acme)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select

from app.db.models import Corretora, InternoAcme
from app.db.session import SessionLocal


@dataclass
class SenderInfo:
    kind: Literal["corretora", "dona", "unknown"]
    id: int | None = None
    nome: str | None = None
    papel: str | None = None


def identify_sender(phone_normalized: str) -> SenderInfo:
    with SessionLocal() as s:
        c = s.execute(
            select(Corretora)
            .where(Corretora.telefone_normalizado == phone_normalized)
            .where(Corretora.ativo.is_(True))
        ).scalar_one_or_none()
        if c:
            return SenderInfo(kind="corretora", id=c.id, nome=c.nome, papel=c.papel)

        i = s.execute(
            select(InternoAcme)
            .where(InternoAcme.telefone_normalizado == phone_normalized)
            .where(InternoAcme.ativa.is_(True))
        ).scalar_one_or_none()
        if i:
            return SenderInfo(kind="dona", id=i.id, nome=i.nome, papel=i.papel)

    return SenderInfo(kind="unknown")
