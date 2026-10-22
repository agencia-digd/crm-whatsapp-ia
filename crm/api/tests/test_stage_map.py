"""Helper stage_map (app/services/stage_map.py) — cache/seed/lookup.

Sem banco: `_load_from_db` é mockado. Testa TTL de cache, degradação em erro,
e os lookups (label_de, stage_por_kommo_id, stages_validos, mapa_kommo).
"""
from __future__ import annotations

import pytest

from app.services import stage_map as sm


_SEED = [
    {"kommo_status_id": "10000008", "twenty_stage": "ENTRADA", "label_pt": "Entrada", "ordem": 1, "ativo": True},
    {"kommo_status_id": "142", "twenty_stage": "CUSTOMER", "label_pt": "Venda", "ordem": 11, "ativo": True},
    {"kommo_status_id": "10000012", "twenty_stage": "VISITA_REAL", "label_pt": "Visita realizada", "ordem": 8, "ativo": True},
]


@pytest.fixture(autouse=True)
def _reset_cache():
    # zera o cache do módulo antes e depois de cada teste
    sm._cache = None
    sm._cache_at = 0.0
    yield
    sm._cache = None
    sm._cache_at = 0.0


def test_stages_ordenados_le_e_cacheia(monkeypatch):
    calls = {"n": 0}

    def fake_load():
        calls["n"] += 1
        return list(_SEED)

    monkeypatch.setattr(sm, "_load_from_db", fake_load)

    a = sm.stages_ordenados()
    b = sm.stages_ordenados()  # dentro do TTL → não recarrega
    assert calls["n"] == 1
    assert [r["twenty_stage"] for r in a] == ["ENTRADA", "CUSTOMER", "VISITA_REAL"]
    assert a == b


def test_cache_expira_e_recarrega(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(sm, "_load_from_db", lambda: (calls.__setitem__("n", calls["n"] + 1), list(_SEED))[1])

    sm.stages_ordenados()
    assert calls["n"] == 1
    # simula passagem do TTL zerando o timestamp
    sm._cache_at = 0.0
    sm.stages_ordenados()
    assert calls["n"] == 2


def test_refresh_invalida(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(sm, "_load_from_db", lambda: (calls.__setitem__("n", calls["n"] + 1), list(_SEED))[1])
    sm.stages_ordenados()
    sm.refresh()
    sm.stages_ordenados()
    assert calls["n"] == 2


def test_degrada_gracioso_em_erro_mantem_cache(monkeypatch):
    monkeypatch.setattr(sm, "_load_from_db", lambda: list(_SEED))
    sm.stages_ordenados()  # popula cache

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(sm, "_load_from_db", boom)
    sm.refresh()  # força recarga → falha → devolve cache velho
    got = sm.stages_ordenados()
    assert [r["twenty_stage"] for r in got] == ["ENTRADA", "CUSTOMER", "VISITA_REAL"]


def test_erro_sem_cache_devolve_vazio(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(sm, "_load_from_db", boom)
    assert sm.stages_ordenados() == []
    assert sm.stages_validos() == set()


def test_label_de(monkeypatch):
    monkeypatch.setattr(sm, "_load_from_db", lambda: list(_SEED))
    assert sm.label_de("VISITA_REAL") == "Visita realizada"
    assert sm.label_de("CUSTOMER") == "Venda"
    assert sm.label_de("NAO_EXISTE") is None
    assert sm.label_de(None) is None


def test_stage_por_kommo_id(monkeypatch):
    monkeypatch.setattr(sm, "_load_from_db", lambda: list(_SEED))
    assert sm.stage_por_kommo_id("142") == "CUSTOMER"
    assert sm.stage_por_kommo_id(142) == "CUSTOMER"  # aceita int
    assert sm.stage_por_kommo_id("999999") is None
    assert sm.stage_por_kommo_id(None) is None


def test_stages_validos_e_mapa_kommo(monkeypatch):
    monkeypatch.setattr(sm, "_load_from_db", lambda: list(_SEED))
    assert sm.stages_validos() == {"ENTRADA", "CUSTOMER", "VISITA_REAL"}
    mapa = sm.mapa_kommo_para_stage()
    assert mapa["142"] == "CUSTOMER"
    assert mapa["10000012"] == "VISITA_REAL"
