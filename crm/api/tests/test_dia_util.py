"""Trava de dia útil nos motores automáticos (porte da DEX, 02/10/2026).

Pronto quando (documento reunioes/2026-10-02-motores-dex-para-acme.md, item 2):
teste com relógio fixo em sábado e domingo mostra 0 envios nos três motores
(cadência, nutrição, mensagem planejada), e na segunda às 9h mostra o envio.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services import cadencia, config_operacional, dia_util, msg_planejada as mp, nutricao

pytestmark = pytest.mark.dia_util

# 03/10/2026 é sábado; 04/10 domingo; 05/10 segunda. 12:00 UTC = 9h em SP.
SAB = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
DOM = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
SEG_9H = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


# ── a regra pura ─────────────────────────────────────────────────────────────

def test_sabado_e_domingo_nao_sao_dia_util():
    assert not dia_util.e_dia_util(SAB)
    assert not dia_util.e_dia_util(DOM)
    assert dia_util.e_dia_util(SEG_9H)


def test_dia_e_o_de_sao_paulo_nao_o_utc():
    # sexta 23:30 em SP = sábado 02:30 UTC → ainda é sexta (dia útil)
    assert dia_util.e_dia_util(datetime(2026, 10, 3, 2, 30, tzinfo=timezone.utc))
    # domingo 23:00 em SP = segunda 02:00 UTC → ainda é domingo
    assert not dia_util.e_dia_util(datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc))


def test_chave_nasce_ligada_e_desliga_pelo_painel(monkeypatch):
    monkeypatch.setattr(config_operacional, "_carregar", lambda: {})
    assert config_operacional.envio_dias_uteis() is True
    assert dia_util.pode_enviar(SAB) is False
    monkeypatch.setattr(config_operacional, "_carregar", lambda: {"envio_dias_uteis": False})
    assert dia_util.pode_enviar(SAB) is True


def test_erro_lendo_a_chave_segura_o_envio(monkeypatch):
    def explode(_):
        raise RuntimeError("banco fora")

    monkeypatch.setattr(config_operacional, "get", explode)
    assert dia_util.trava_ligada() is True
    assert dia_util.pode_enviar(DOM) is False


# ── os três motores com relógio fixo ─────────────────────────────────────────

class _SessaoProibida:
    """Fim de semana: o tick tem que sair ANTES de abrir o banco."""

    def __getattr__(self, nome):
        raise AssertionError(f"tick tocou no banco no fim de semana ({nome})")


class _Sender:
    def __init__(self):
        self.chamadas = 0

    def __call__(self, *a, **k):
        self.chamadas += 1
        return {"ok": True, "dry_run": True, "sent": False, "wamid": "fake"}


@pytest.fixture()
def chave_ligada(monkeypatch):
    monkeypatch.setattr(config_operacional, "_carregar", lambda: {})
    monkeypatch.setattr(config_operacional, "cadencia_ativa", lambda: True)
    monkeypatch.setattr(config_operacional, "nutricao_ativa", lambda: True)


@pytest.mark.parametrize("quando", [SAB, DOM])
def test_fim_de_semana_zero_envio_nos_tres_motores(chave_ligada, quando, monkeypatch):
    s_cad, s_nut, s_msg = _Sender(), _Sender(), _Sender()
    r1 = cadencia.tick(agora=quando, session=_SessaoProibida(), sender=s_cad)
    r2 = nutricao.tick(agora=quando, session=_SessaoProibida(), sender=s_nut)
    monkeypatch.setattr(mp, "_now", lambda: quando)
    r3 = mp.tick(session=_SessaoProibida(), sender=s_msg)
    assert (s_cad.chamadas, s_nut.chamadas, s_msg.chamadas) == (0, 0, 0)
    assert r1.considerados == 0 and r2.marcos_disparados == 0 and r3.considerados == 0


# Segunda 9h: o mesmo estado que ficou devido no fim de semana SAI.
from tests.test_cadencia import (  # noqa: E402 — reaproveita o banco SQLite de lá
    FakeSender as FakeSenderCadencia,
    session_factory as session_factory_cadencia,  # noqa: F401 — fixture
)


def test_cadencia_devida_no_sabado_sai_na_segunda(chave_ligada, session_factory_cadencia):
    with session_factory_cadencia() as s:
        cadencia.iniciar_cadencia(s, telefone="+5500000000000", lead_id="k1",
                                  corretora_id=3, agora=SAB - timedelta(hours=1))
        s.commit()
    for quando in (SAB, DOM):
        sender = FakeSenderCadencia()
        cadencia._LAST_ENQUEUE["at"] = 0.0
        with session_factory_cadencia() as s:
            cadencia.tick(agora=quando, session=s, sender=sender)
        assert sender.chamadas == [], f"cadência mandou em {quando:%a}"
    sender = FakeSenderCadencia()
    cadencia._LAST_ENQUEUE["at"] = 0.0
    with session_factory_cadencia() as s:
        res = cadencia.tick(agora=SEG_9H, session=s, sender=sender)
    assert len(sender.chamadas) == 1, "o toque segurado no fim de semana não saiu na segunda"
    assert res.mensagens_enfileiradas == 1


from tests.test_nutricao import (  # noqa: E402
    FakeSender as FakeSenderNutricao,
    _seed_remarketing,
    session_factory as session_factory_nutricao,  # noqa: F401 — fixture
)


def test_marco_da_nutricao_devido_no_domingo_sai_na_segunda(chave_ligada, session_factory_nutricao):
    s = session_factory_nutricao()
    # pós-cadência com o marco vencendo no sábado de manhã
    _seed_remarketing(s, proximo=SAB - timedelta(hours=2))
    for quando in (SAB, DOM):
        sender = FakeSenderNutricao()
        nutricao.tick(agora=quando, session=s, sender=sender)
        assert sender.chamadas == [], f"nutrição mandou em {quando:%a}"
    sender = FakeSenderNutricao()
    cadencia._LAST_ENQUEUE["at"] = 0.0
    res = nutricao.tick(agora=SEG_9H, session=s, sender=sender)
    assert len(sender.chamadas) == 1 and res.marcos_disparados == 1


def test_msg_planejada_segura_no_fim_de_semana_e_sai_na_segunda(chave_ligada, monkeypatch):
    """Banco de teste (Postgres :5433), mesmo semeador do test_msg_planejada."""
    from app.services import cloud_lines as cl
    from tests import test_msg_planejada as tmp

    class LinhaFake:
        slug = "corretor"

    monkeypatch.setattr(cl, "linha_da_corretora", lambda cid: LinhaFake())
    monkeypatch.setattr(mp, "_cap_atingido", lambda s: False)
    tmp._limpar()
    try:
        tmp._semear(tmp.SessionLocal, dias_offset=-3)
        for quando in (SAB, DOM):
            monkeypatch.setattr(mp, "_now", lambda q=quando: q)
            sender = tmp.FakeSender()
            with tmp.SessionLocal() as s:
                mp.tick(session=s, sender=sender)
            assert sender.chamadas == [], f"mensagem planejada saiu em {quando:%a}"
        monkeypatch.setattr(mp, "_now", lambda: SEG_9H)
        sender = tmp.FakeSender()
        with tmp.SessionLocal() as s:
            mp.tick(session=s, sender=sender)
        assert len(sender.chamadas) == 1
    finally:
        tmp._limpar()
