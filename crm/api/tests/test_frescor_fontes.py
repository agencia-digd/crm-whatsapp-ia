"""Testes — carimbo de frescor (_frescor_fontes) do painel.

Cobre F5/P0.2 da Revisão-02-Funil: o bloco `fontes` que carimba o "dados de
quando" de cada fonte das telas do imob.

GUARDRAIL: estes testes NUNCA tocam o banco. `painel.SessionLocal` é mockado
por um fake que devolve timestamps controlados — nenhuma conexão real é aberta.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.api import painel


class _FakeSession:
    """Sessão fake: mapeia SQL → valor via substring match no texto do MAX()."""

    def __init__(self, valores_por_tabela: dict[str, Any]):
        self._valores = valores_por_tabela

    def execute(self, stmt: Any) -> MagicMock:
        sql = str(stmt).lower()
        val = None
        for tabela, v in self._valores.items():
            if tabela in sql:
                val = v
                break
        m = MagicMock()
        m.scalar.return_value = val
        return m


def _fake_session_local(valores: dict[str, Any]):
    @contextmanager
    def _cm():
        yield _FakeSession(valores)
    factory = MagicMock()
    factory.side_effect = lambda: _cm()
    return factory


def _reset_cache() -> None:
    painel._FRESCOR_CACHE["ts"] = 0.0
    painel._FRESCOR_CACHE["data"] = None


@pytest.fixture(autouse=True)
def _limpa_cache():
    _reset_cache()
    yield
    _reset_cache()


def test_shape_completo_com_timestamps() -> None:
    """Todas as 5 fontes retornam ISO UTC quando o banco tem dados."""
    dt = datetime(2026, 7, 1, 19, 32, tzinfo=timezone.utc)
    valores = {
        "lead_analyses": dt,
        "leads_snapshot": datetime(2026, 7, 1, 7, 0, tzinfo=timezone.utc),
        "mensagens_whatsapp": datetime(2026, 7, 1, 20, 15, tzinfo=timezone.utc),
        "vendas_mensais": datetime(2026, 6, 30, 23, 0, tzinfo=timezone.utc),
        "atendimentos_evolution_mensais": datetime(2026, 7, 1, 3, 0, tzinfo=timezone.utc),
    }
    with patch.object(painel, "SessionLocal", _fake_session_local(valores)):
        out = painel._frescor_fontes()

    assert set(out.keys()) == {
        "analises", "snapshot_kommo", "conversas",
        "vendas_mensais", "atendidos_evolution",
    }
    assert out["analises"] == "2026-07-01T19:32:00+00:00"
    assert out["snapshot_kommo"] == "2026-07-01T07:00:00+00:00"
    assert out["conversas"] == "2026-07-01T20:15:00+00:00"
    assert out["vendas_mensais"] == "2026-06-30T23:00:00+00:00"
    assert out["atendidos_evolution"] == "2026-07-01T03:00:00+00:00"
    # Todos os valores são str ISO ou None (contrato do bloco).
    for v in out.values():
        assert v is None or isinstance(v, str)


def test_naive_datetime_normaliza_pra_utc() -> None:
    """Timestamp naive (sem tz) é assumido UTC e ganha sufixo +00:00."""
    valores = {"lead_analyses": datetime(2026, 7, 1, 12, 0)}  # naive
    with patch.object(painel, "SessionLocal", _fake_session_local(valores)):
        out = painel._frescor_fontes()
    assert out["analises"] == "2026-07-01T12:00:00+00:00"


def test_fonte_vazia_vira_none_sem_quebrar() -> None:
    """Tabela sem linhas (MAX → None) devolve None, mantendo o shape."""
    with patch.object(painel, "SessionLocal", _fake_session_local({})):
        out = painel._frescor_fontes()
    assert set(out.keys()) == {
        "analises", "snapshot_kommo", "conversas",
        "vendas_mensais", "atendidos_evolution",
    }
    assert all(v is None for v in out.values())


def test_cache_60s_evita_segunda_query() -> None:
    """Segunda chamada dentro de 60s reusa cache (não reabre sessão)."""
    valores = {"lead_analyses": datetime(2026, 7, 1, 19, 0, tzinfo=timezone.utc)}
    factory = _fake_session_local(valores)
    with patch.object(painel, "SessionLocal", factory):
        painel._frescor_fontes()
        chamadas_1 = factory.call_count
        painel._frescor_fontes()
        chamadas_2 = factory.call_count
    # A 2ª chamada não deve reabrir SessionLocal (cache quente).
    assert chamadas_1 >= 1
    assert chamadas_2 == chamadas_1


def test_falha_de_sessao_degrada_gracioso() -> None:
    """Se abrir sessão explode, devolve shape completo com todas None."""
    factory = MagicMock(side_effect=RuntimeError("db down"))
    with patch.object(painel, "SessionLocal", factory):
        out = painel._frescor_fontes()
    assert set(out.keys()) == {
        "analises", "snapshot_kommo", "conversas",
        "vendas_mensais", "atendidos_evolution",
    }
    assert all(v is None for v in out.values())
