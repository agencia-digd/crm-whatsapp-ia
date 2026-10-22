"""custo_usd em llm_chamadas: tabela de preços + gravação pelo ContabilProvider.

DB: SQLite in-memory com a tabela criada à mão (llm_chamadas só existe por migration).
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.db.session as sessao
from app.llm.contabil_provider import ContabilProvider
from app.llm.precos import custo_usd
from app.llm.provider import TokenUsage

_COLS = ("id INTEGER PRIMARY KEY AUTOINCREMENT, servico TEXT, provider TEXT, modelo TEXT, "
         "input_tokens INT, output_tokens INT, duracao_ms INT, erro TEXT")


class _Fake:
    model = "claude-haiku-4-5-20251001"

    def __init__(self, falha=False):
        self.falha = falha

    def complete(self, system, user, json_schema=None):
        if self.falha:
            raise RuntimeError("quebrou")
        return "{}", TokenUsage(input_tokens=2000, output_tokens=500)


def _banco(monkeypatch, com_coluna: bool):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with eng.begin() as c:
        c.execute(text(f"CREATE TABLE llm_chamadas ({_COLS}{', custo_usd NUMERIC' if com_coluna else ''})"))
    fab = sessionmaker(bind=eng)
    monkeypatch.setattr(sessao, "SessionLocal", fab)
    return fab


def test_precos_por_modelo():
    assert custo_usd("claude-haiku-4-5-20251001", 1_000_000, 1_000_000) == 6.0
    assert custo_usd("claude-sonnet-4-6", 2000, 500) == pytest.approx(0.0135)
    assert custo_usd("gpt-5.6-terra", 1000, 1000) is None   # sem preço: NULL, nunca 0
    assert custo_usd(None, 1, 1) is None and custo_usd("?", 1, 1) is None


def test_contabil_grava_custo(monkeypatch):
    fab = _banco(monkeypatch, com_coluna=True)
    ContabilProvider(_Fake()).complete("s", "u")
    with fab() as s:
        r = s.execute(text("SELECT modelo, input_tokens, custo_usd FROM llm_chamadas")).one()
    assert r[0].startswith("claude-haiku-4-5") and r[1] == 2000
    assert float(r[2]) == pytest.approx(0.0045)   # 2000*1/1e6 + 500*5/1e6


def test_contabil_modelo_sem_preco_fica_null(monkeypatch):
    fab = _banco(monkeypatch, com_coluna=True)
    f = _Fake(); f.model = "gpt-5.6-terra"
    ContabilProvider(f).complete("s", "u")
    with fab() as s:
        assert s.execute(text("SELECT custo_usd FROM llm_chamadas")).scalar() is None


def test_codigo_novo_antes_da_migration_nao_perde_a_chamada(monkeypatch):
    fab = _banco(monkeypatch, com_coluna=False)
    ContabilProvider(_Fake()).complete("s", "u")
    with fab() as s:
        assert s.execute(text("SELECT count(*) FROM llm_chamadas")).scalar() == 1


def test_chamada_com_erro_tambem_grava(monkeypatch):
    fab = _banco(monkeypatch, com_coluna=True)
    with pytest.raises(RuntimeError):
        ContabilProvider(_Fake(falha=True)).complete("s", "u")
    with fab() as s:
        r = s.execute(text("SELECT erro, custo_usd FROM llm_chamadas")).one()
    assert "quebrou" in r[0] and float(r[1]) == 0.0
