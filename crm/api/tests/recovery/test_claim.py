"""Teste do claim atômico do recovery (Codex #4) — usa DB real.

Pulado quando TEST_DATABASE_URL não está setada (não roda contra produção).
Prova que dois claims do MESMO lead_id não podem ambos ganhar: o 1º cria a
reserva (True), o 2º vê o conflito e perde (False) — logo, duas waves
concorrentes nunca despacham 2 HSMs pro mesmo lead.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="precisa de TEST_DATABASE_URL (DB de teste real)",
)

_LID = "claim-test-lead-1"


def _cleanup():
    from sqlalchemy import text

    from app.db.session import SessionLocal

    with SessionLocal() as s:
        s.execute(text("DELETE FROM recovery_conversations WHERE lead_id = :l"), {"l": _LID})
        s.commit()


def test_claim_new_e_atomico_por_lead_id():
    from app.db.session import SessionLocal
    from app.recovery import repo

    _cleanup()
    try:
        with SessionLocal() as s:
            ganhou = repo.claim_new(s, lead_id=_LID, lead_phone="+5500000000000", lead_name="X")
        assert ganhou is True  # 1ª wave reserva

        with SessionLocal() as s:
            perdeu = repo.claim_new(s, lead_id=_LID, lead_phone="+5500000000000", lead_name="X")
        assert perdeu is False  # 2ª wave vê o conflito e não reenvia
    finally:
        _cleanup()
