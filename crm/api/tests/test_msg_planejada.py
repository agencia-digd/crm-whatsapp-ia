"""Mensagem planejada — o caminho que ficou MORTO em produção.

Contexto (29/07/2026): o patch de roteamento por linha (6fdd27a4) escreveu
`row.get("corretora_id")` dentro de um loop que itera em `t`. `row` não existia
naquele escopo → NameError → o `except` genérico engolia → rollback → NENHUMA
mensagem planejada era enviada. Ficou assim de um dia pro outro, em produção.

O que dói: a suíte tinha 1.326 testes passando com o bug de pé. Nenhum cobria
este caminho. Quem achou foi uma auditoria externa (Sol), não o CI.

Estes testes existem pra isso não voltar. O primeiro é o que teria pegado:
exercita o tick de ponta a ponta com uma tarefa devida e exige que o sender
seja CHAMADO — qualquer exceção silenciada nesse trecho quebra o teste.

DB: Postgres de TESTE (TEST_DATABASE_URL, preparado pelo conftest). SQLite não
serve aqui — o SQL do módulo usa `AT TIME ZONE`, `regexp_replace` e `btrim`, que
são de Postgres. Cada teste limpa o que semeia. Sender injetado: zero envio.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sql_text

from app.db.session import SessionLocal
from app.services import msg_planejada as mp


class FakeSender:
    """Registra o que seria enviado. NUNCA envia."""

    def __init__(self):
        self.chamadas: list[dict] = []

    def __call__(self, telefone, first_name=None, hook=None, **kw):
        self.chamadas.append({"telefone": telefone, "hook": hook, **kw})
        return {"ok": True, "dry_run": True, "sent": False, "wamid": "fake"}


@pytest.fixture()
def session_factory():
    """Postgres de teste + limpeza do que este arquivo semeia."""
    _limpar()
    yield SessionLocal
    _limpar()


def _limpar() -> None:
    with SessionLocal() as s:
        s.execute(sql_text("DELETE FROM tarefas_corretora WHERE texto = 'teste-msgplanejada'"))
        s.execute(sql_text("DELETE FROM leads_snapshot WHERE lead_id LIKE 'tstmp_%'"))
        s.commit()


def _semear(factory, *, corretora_id: int | None = 3, dias_offset: int = -1) -> str:
    """Tarefa com mensagem planejada JÁ DEVIDA (ou futura) + lead com telefone."""
    lead_id = f"tstmp_{uuid.uuid4().hex[:8]}"
    with factory() as s:
        s.execute(
            sql_text(
                """
                INSERT INTO leads_snapshot (lead_id, nome, telefone, corretora_id)
                VALUES (:lid, 'Lead Teste', '+5500000000000', :cid)
                """
            ),
            {"lid": lead_id, "cid": corretora_id},
        )
        s.execute(
            sql_text(
                """
                INSERT INTO tarefas_corretora
                    (tenant_id, lead_id, lead_nome, corretora_id, data_tarefa,
                     hora_tarefa, texto, status, mensagem_planejada)
                VALUES
                    (1, :lid, 'Lead Teste', :cid, :dia, TIME '09:00',
                     'teste-msgplanejada', 'aberta',
                     'Passando pra saber se ainda faz sentido?')
                """
            ),
            {
                "lid": lead_id,
                "cid": corretora_id,
                "dia": (datetime.now(timezone.utc) + timedelta(days=dias_offset)).date(),
            },
        )
        s.commit()
    return lead_id


# ── 1. O teste que teria pego o bug ──────────────────────────────────────────

def test_tarefa_devida_realmente_dispara_o_sender(session_factory, monkeypatch):
    """REGRESSÃO (29/07): NameError no roteamento por linha matava o envio.

    Se qualquer exceção for silenciada no meio do loop, o sender não é chamado
    e este teste falha. É exatamente o sintoma que passou despercebido em prod.
    """
    from app.services import cloud_lines as cl

    class LinhaFake:
        slug = "corretor"

    monkeypatch.setattr(cl, "linha_da_corretora", lambda cid: LinhaFake())
    monkeypatch.setattr(mp, "_cap_atingido", lambda s: False)

    _semear(session_factory)
    sender = FakeSender()

    with session_factory() as s:
        res = mp.tick(session=s, sender=sender)

    assert sender.chamadas, (
        "o sender NÃO foi chamado — alguma exceção está sendo engolida no loop "
        "(foi assim que o NameError de `row` passou batido)"
    )
    assert res.considerados == 1


def test_erro_no_loop_nao_fica_invisivel(session_factory, monkeypatch):
    """Se o roteamento explodir, tem que CONTAR o erro — não sumir em silêncio.

    O bug original virava um no-op mudo: nada enviado, nada contado, nada
    gritando. O resultado do tick precisa dar sinal.
    """
    from app.services import cloud_lines as cl

    def explode(cid):
        raise RuntimeError("registry fora do ar")

    monkeypatch.setattr(cl, "linha_da_corretora", explode)
    monkeypatch.setattr(mp, "_cap_atingido", lambda s: False)

    _semear(session_factory)
    sender = FakeSender()

    with session_factory() as s:
        res = mp.tick(session=s, sender=sender)

    assert not sender.chamadas
    assert res.considerados == 1
    # o tick tem que registrar que algo deu errado, de alguma forma
    contadores = {
        k: v for k, v in vars(res).items() if isinstance(v, int) and k != "considerados"
    }
    assert any(v > 0 for v in contadores.values()), (
        f"tarefa considerada mas nenhum contador subiu — erro invisível: {contadores}"
    )


# ── 2. Travas que já existiam (guardando pra não regredir junto) ─────────────

def test_sem_linha_cloud_nao_envia(session_factory, monkeypatch):
    """Sem linha da dona, PULA — melhor não enviar do que enviar pelo nº errado."""
    from app.services import cloud_lines as cl

    monkeypatch.setattr(cl, "linha_da_corretora", lambda cid: None)
    monkeypatch.setattr(mp, "_cap_atingido", lambda s: False)

    _semear(session_factory)
    sender = FakeSender()
    with session_factory() as s:
        mp.tick(session=s, sender=sender)
    assert not sender.chamadas


def test_cap_diario_bloqueia(session_factory, monkeypatch):
    from app.services import cloud_lines as cl

    class LinhaFake:
        slug = "corretor"

    monkeypatch.setattr(cl, "linha_da_corretora", lambda cid: LinhaFake())
    monkeypatch.setattr(mp, "_cap_atingido", lambda s: True)

    _semear(session_factory)
    sender = FakeSender()
    with session_factory() as s:
        res = mp.tick(session=s, sender=sender)
    assert not sender.chamadas
    assert res.bloqueados_cap == 1


def test_tarefa_futura_nao_e_considerada(session_factory, monkeypatch):
    from app.services import cloud_lines as cl

    class LinhaFake:
        slug = "corretor"

    monkeypatch.setattr(cl, "linha_da_corretora", lambda cid: LinhaFake())
    monkeypatch.setattr(mp, "_cap_atingido", lambda s: False)

    _semear(session_factory, dias_offset=+2)  # daqui a 2 dias
    sender = FakeSender()
    with session_factory() as s:
        res = mp.tick(session=s, sender=sender)
    assert res.considerados == 0
    assert not sender.chamadas


# ── 3. Trava contra tick sobreposto (auditoria Grok+Sol, 29/07) ─────────────

def test_tick_concorrente_nao_envia(session_factory, monkeypatch):
    """Dois ticks sobrepostos liam a MESMA tarefa (busca → envia → só então
    marca `msg_planejada_enviada_em`) e o lead recebia 2×."""
    from app.services import trava_tick

    monkeypatch.setattr(mp, "travar", lambda ns, t: trava_tick.OCUPADO)
    _semear(session_factory)
    sender = FakeSender()
    with session_factory() as s:
        res = mp.tick(session=s, sender=sender)
    assert not sender.chamadas, "enviou com outro tick rodando"
    assert res.considerados == 0
