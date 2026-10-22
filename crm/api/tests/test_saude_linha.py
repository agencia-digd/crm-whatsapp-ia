"""Saúde da linha na tela — "o celular está vermelho?".

Duas regras que estes testes seguram:
1. Número sem hora de leitura vira número velho tratado como verdade: quando a
   Meta não responde, a tela recebe a última leitura boa COM A HORA DELA.
2. Sem leitura não é verde.
"""
from __future__ import annotations

import pytest

from app.services import saude_linha as sl


@pytest.fixture(autouse=True)
def _cache_limpo():
    sl.esquecer()
    yield
    sl.esquecer()


def _leitura(qualidade="GREEN", status="CONNECTED", lido_em="2026-10-05T12:00:00+00:00"):
    return {
        "slug": "corretor", "phone_number_id": "000000000000000",
        "numero": "+5500000000000", "nome": "Acme Imóveis",
        "qualidade": qualidade, "status": status,
        "cor": sl.cor_de(qualidade, status), "lido_em": lido_em,
    }


@pytest.mark.parametrize("qualidade,status,cor", [
    ("GREEN", "CONNECTED", "verde"),
    ("YELLOW", "CONNECTED", "amarelo"),
    ("RED", "CONNECTED", "vermelho"),
    ("GREEN", "FLAGGED", "vermelho"),      # status ruim ganha da qualidade
    ("GREEN", "RESTRICTED", "vermelho"),
    ("UNKNOWN", "CONNECTED", "desconhecida"),
    (None, None, "desconhecida"),
])
def test_cor(qualidade, status, cor):
    assert sl.cor_de(qualidade, status) == cor


def test_leitura_boa_traz_cor_e_hora(monkeypatch):
    monkeypatch.setattr(sl, "_buscar", lambda slug: _leitura("RED"))
    r = sl.ler("corretor")
    assert r["cor"] == "vermelho"
    assert r["lido_em"] == "2026-10-05T12:00:00+00:00"
    assert r["velha"] is False and r["erro"] is None


def test_cache_de_5_min_nao_bate_na_meta_a_cada_render(monkeypatch):
    chamadas = []
    monkeypatch.setattr(sl, "_buscar", lambda slug: chamadas.append(1) or _leitura())
    for _ in range(5):
        sl.ler("corretor")
    assert len(chamadas) == 1
    assert sl.TTL_OK_S == 300


def test_cache_vence(monkeypatch):
    chamadas = []
    monkeypatch.setattr(sl, "_buscar", lambda slug: chamadas.append(1) or _leitura())
    relogio = [1000.0]
    monkeypatch.setattr(sl.time, "monotonic", lambda: relogio[0])
    sl.ler("corretor")
    relogio[0] += sl.TTL_OK_S + 1
    sl.ler("corretor")
    assert len(chamadas) == 2


def test_falha_devolve_a_ultima_leitura_com_a_hora_DELA(monkeypatch):
    """A Meta caiu depois de uma leitura RED: a tela continua dizendo RED, com
    a hora em que isso foi lido e o aviso de que está velho. Nunca hora nova."""
    relogio = [1000.0]
    monkeypatch.setattr(sl.time, "monotonic", lambda: relogio[0])
    monkeypatch.setattr(sl, "_buscar", lambda slug: _leitura("RED"))
    sl.ler("corretor")

    def _cai(slug):
        raise RuntimeError("timeout")

    relogio[0] += sl.TTL_OK_S + 1
    monkeypatch.setattr(sl, "_buscar", _cai)
    r = sl.ler("corretor")
    assert r["cor"] == "vermelho"
    assert r["lido_em"] == "2026-10-05T12:00:00+00:00"
    assert r["velha"] is True
    assert "timeout" in r["erro"]


def test_sem_leitura_nenhuma_nao_e_verde(monkeypatch):
    def _cai(slug):
        raise RuntimeError("sem token")

    monkeypatch.setattr(sl, "_buscar", _cai)
    r = sl.ler("corretor")
    assert r["cor"] == "desconhecida"
    assert r["lido_em"] is None
    assert r["erro"]


def test_falha_tenta_de_novo_logo_e_nao_em_5_min(monkeypatch):
    relogio = [1000.0]
    monkeypatch.setattr(sl.time, "monotonic", lambda: relogio[0])
    chamadas = []

    def _cai(slug):
        chamadas.append(1)
        raise RuntimeError("rede")

    monkeypatch.setattr(sl, "_buscar", _cai)
    sl.ler("corretor")
    relogio[0] += sl.TTL_ERRO_S + 1
    sl.ler("corretor")
    assert len(chamadas) == 2


def test_a_suite_nao_fala_com_a_meta():
    """Sem trocar `_buscar`, a leitura real é bloqueada sob pytest."""
    r = sl.ler("corretor")
    assert r["cor"] == "desconhecida"
    assert "bloqueada" in r["erro"]


def test_token_vai_no_cabecalho_e_nao_na_url():
    """`access_token=` na query acaba em mensagem de exceção e em log."""
    import inspect

    fonte = inspect.getsource(sl._buscar)
    assert "Authorization" in fonte
    assert "access_token" not in fonte


def test_o_mapa_da_tela_publica_saude_e_agenda(monkeypatch):
    from app.api import automacoes

    monkeypatch.setattr(sl, "_buscar", lambda slug: _leitura("RED"))
    mapa = automacoes._mapa_carrossel_corretor()
    assert mapa["saude_linha"]["cor"] == "vermelho"
    assert mapa["saude_linha"]["lido_em"]
    assert "config" in mapa["agenda"]
    cfg_agenda = mapa["agenda"]["config"]
    assert cfg_agenda is not None, mapa["agenda"]
    for campo in ("janela_horario", "dias_semana", "cap_diario", "cap_semanal",
                  "enviados_semana", "saldo_semana", "bloqueio_agora", "timer"):
        assert campo in cfg_agenda
