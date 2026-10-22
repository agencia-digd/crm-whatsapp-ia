"""A suíte não encosta em produção — as travas do conftest, provadas.

Três portas por onde um `pytest` chegava (ou chegaria) no ambiente vivo:

  1. o gate do emissor: pasta fixa em /var/lib/copiloto-emission-gate, com o
     arquivo de pausa que o operador usa pra parar os envios de verdade;
  2. a consulta de categoria de template: GET na Graph da Meta com o token da
     linha, gastando a cota do app e deixando o teste na mão da Meta;
  3. o cache dessa consulta em /var/lib/copiloto/linhas_utility.json;
  4. o Kommo: 12 testes abriam conexão de verdade, um deles na conta real com
     o token de /opt/acme-kommo/.env.

Se alguém afrouxar o conftest, é aqui que cai — antes de um teste pausar (ou
despausar) o emissor da Acme.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services import cloud_lines, emission_gate

_GATE_PRODUCAO = Path("/var/lib/copiloto-emission-gate")
_CACHE_PRODUCAO = Path("/var/lib/copiloto/linhas_utility.json")


def test_gate_do_emissor_nao_aponta_pra_producao():
    assert emission_gate.DIRECTORY.resolve() != _GATE_PRODUCAO
    assert _GATE_PRODUCAO not in emission_gate.DIRECTORY.resolve().parents


def test_gate_da_suite_admite_envio_e_respeita_a_pausa():
    """O gate continua sendo o código de produção, só que noutra pasta: aberto
    admite, pausado recusa, e retomar reabre."""
    with emission_gate.admission():
        pass

    assert emission_gate.set_paused(True) == {"paused": True, "inflight_drained": True}
    with pytest.raises(emission_gate.EmissionPaused):
        with emission_gate.admission():
            pass

    emission_gate.set_paused(False)
    with emission_gate.admission():
        pass
    # tudo o que o gate escreveu ficou na pasta da suíte
    assert (emission_gate.DIRECTORY / "paused").read_text().strip() == "0"


def test_pausa_de_um_teste_nao_vaza_pro_seguinte_parte_1():
    emission_gate.set_paused(True)


def test_pausa_de_um_teste_nao_vaza_pro_seguinte_parte_2():
    """Roda logo depois da parte 1: a fixture do conftest zerou a pausa."""
    with emission_gate.admission():
        pass


def test_categoria_de_template_nao_vai_a_meta():
    """Sem rede: 'não deu pra saber' (None), e as duas leituras ficam vazias."""
    assert cloud_lines._buscar_categoria_na_meta("corretor", "UTILITY") is None
    assert cloud_lines._buscar_categoria_na_meta("corretor", "MARKETING") is None
    assert cloud_lines.templates_utility("corretor") == set()
    assert cloud_lines.templates_marketing("corretor") == set()


def test_cache_de_categoria_nao_e_o_de_producao():
    assert Path(cloud_lines._CAT_ARQUIVO).resolve() != _CACHE_PRODUCAO


def test_kommo_nao_sai_pra_rede(monkeypatch):
    """Pedido pra *.kommo.com volta 401 do conftest — e nenhum socket abre."""
    import socket

    import httpx

    def _proibido(*a, **k):
        raise AssertionError("a suíte tentou abrir conexão com o Kommo")

    monkeypatch.setattr(socket.socket, "connect", _proibido)
    monkeypatch.setattr(socket, "getaddrinfo", _proibido)
    for url in ("https://example.kommo.com/api/v4/events",
                "https://x.kommo.com/api/v4/account"):
        r = httpx.get(url, headers={"Authorization": "Bearer nao-e-token"})
        assert r.status_code == 401
        assert "conftest" in r.json()["detail"]


def test_painel_nao_busca_evento_no_kommo_de_verdade(monkeypatch):
    """O caminho que consultava a conta real: com a guarda, devolve lista vazia."""
    from datetime import date

    from app.api import painel

    monkeypatch.setattr(painel, "_kommo_token", lambda: "nao-e-token")
    painel._KOMMO_EVENTS_CACHE.clear()
    try:
        assert painel._kommo_fetch_status_events(date(2026, 1, 1), date(2026, 1, 2)) == []
    finally:
        painel._KOMMO_EVENTS_CACHE.clear()
