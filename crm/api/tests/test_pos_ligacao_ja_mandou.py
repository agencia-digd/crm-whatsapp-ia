"""`_ja_mandou_hoje` não pode contar o que a Meta RECUSOU.

Em 21/09/2026 três pós-ligação da Pessoa7 (leads 1040, 1041, 1073) voltaram
`delivery_status='failed'` e ficaram presos: a trava anti-duplicata via o
registro no banco e devolvia "já mandou", sem olhar se a mensagem tinha saído.
A pessoa não recebeu nada e o reenvio legítimo só aconteceu com contorno manual.

O que está sendo fixado aqui:
  - `failed`            -> NÃO conta, pode reenviar
  - `delivered`/`read`/`sent`/`played` -> conta, não reenvia
  - `NULL`              -> conta. A Meta aceitou e devolveu `wamid`; o status
                           ainda não voltou pelo webhook. Reenviar aí duplica
                           de verdade, numa pessoa real.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sql_text

from app.db.session import SessionLocal
from app.services.pos_ligacao import _ja_mandou_hoje

LEAD_BASE = 987_650_000  # faixa fora do uso real, pra não colidir com lead de verdade


def _registra(session, lead_id: int, status: str | None, *, horas_atras: float = 1.0) -> None:
    meta: dict[str, object] = {"origem": "pos_ligacao", "lead_id": str(lead_id)}
    if status is not None:
        meta["delivery_status"] = status
    session.execute(
        sql_text(
            """
            INSERT INTO mensagens_whatsapp
                (id, tenant_id, evolution_message_id, instance, remote_jid,
                 from_corretora, tipo_mensagem, conteudo_metadata, timestamp_evolution)
            VALUES
                (gen_random_uuid(), 1, :mid, 'teste', '+5500000000000@s.whatsapp.net',
                 true, 'template', CAST(:meta AS jsonb), :ts)
            """
        ),
        {
            "mid": "teste-%s" % uuid.uuid4(),
            "meta": __import__("json").dumps(meta),
            "ts": datetime.now(timezone.utc) - timedelta(hours=horas_atras),
        },
    )


@pytest.fixture
def sessao():
    with SessionLocal() as s:
        yield s
        s.rollback()


@pytest.mark.parametrize(
    "status, espera_bloqueio",
    [
        ("failed", False),      # o furo que custou três leads
        ("delivered", True),
        ("read", True),
        ("sent", True),
        ("played", True),
        (None, True),           # aceito pela Meta, status ainda não voltou
    ],
)
def test_status_decide_se_bloqueia(sessao, status, espera_bloqueio):
    lead = LEAD_BASE + abs(hash(str(status))) % 10_000
    sessao.execute(
        sql_text("DELETE FROM mensagens_whatsapp WHERE conteudo_metadata->>'lead_id' = :l"),
        {"l": str(lead)},
    )
    _registra(sessao, lead, status)
    assert _ja_mandou_hoje(sessao, lead) is espera_bloqueio


def test_falha_seguida_de_entrega_ainda_bloqueia(sessao):
    """Reenviou depois da falha e deu certo: a segunda tentativa trava a terceira."""
    lead = LEAD_BASE + 1
    sessao.execute(
        sql_text("DELETE FROM mensagens_whatsapp WHERE conteudo_metadata->>'lead_id' = :l"),
        {"l": str(lead)},
    )
    _registra(sessao, lead, "failed", horas_atras=2)
    _registra(sessao, lead, "delivered", horas_atras=1)
    assert _ja_mandou_hoje(sessao, lead) is True


def test_falha_antiga_fora_da_janela_nao_conta(sessao):
    lead = LEAD_BASE + 2
    sessao.execute(
        sql_text("DELETE FROM mensagens_whatsapp WHERE conteudo_metadata->>'lead_id' = :l"),
        {"l": str(lead)},
    )
    _registra(sessao, lead, "delivered", horas_atras=30)
    assert _ja_mandou_hoje(sessao, lead) is False
