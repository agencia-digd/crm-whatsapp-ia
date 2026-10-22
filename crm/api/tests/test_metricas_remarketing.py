"""Métricas do remarketing (carrossel da Corretor) — a régua honesta de leitura.

Mesmas travas das métricas da cadência, porque é a mesma tela e a mesma pessoa
lendo: `None` = sem dado (nunca 0%), amostra pequena marcada, marco de régua
respeitado, e o que não é variante de hoje fica FORA do placar.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.api import automacoes as api
from app.services import carrossel_copies as cc


@pytest.fixture
def sessao():
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        yield s


def _metricas(sessao, monkeypatch, linhas, marco=None):
    from app.services import carrossel_corretor as cb

    monkeypatch.setattr(cb, "placar", lambda *a, **k: linhas)
    monkeypatch.setattr(api, "_ultimo_marco", lambda s: marco)
    monkeypatch.setattr(api, "_estoque_carrossel",
                        lambda s: {"fila": 120, "disparado": 40, "respondeu": 7})
    return api.calcular_metricas_remarketing(sessao, dias=30)


def test_sem_envio_e_sem_dado_nao_zero(sessao, monkeypatch):
    out = _metricas(sessao, monkeypatch, [])
    for v in out["variantes"]:
        assert v["taxa"] is None, "sem envio tem que ser 'sem dado', não 0%"
        assert v["sem_dado"] is True
    assert out["total"]["taxa"] is None


def test_taxa_por_variante(sessao, monkeypatch):
    linhas = [
        {"variante": "direta", "categoria": "UTILITY", "enviados": 100,
         "respostas": 13},
        {"variante": "brendon_triagem", "categoria": "UTILITY", "enviados": 50,
         "respostas": 5},
    ]
    out = _metricas(sessao, monkeypatch, linhas)
    por_id = {v["id"]: v for v in out["variantes"]}
    assert por_id["direta"]["taxa"] == 13.0
    assert por_id["brendon_triagem"]["taxa"] == 10.0
    assert out["total"]["enviados"] == 150
    assert out["total"]["respostas"] == 18


def test_respostas_por_tipo_separa_interesse_de_botao(sessao, monkeypatch):
    """A tela mostra seis tipos. `interesse` + `botao` fecham com `respostas`
    (o que entra na taxa); o resto fica fora dela."""
    linhas = [
        {"variante": "direta", "categoria": "UTILITY", "enviados": 100,
         "respostas": 13, "botoes": 4, "recusas": 6, "pararam": 2,
         "indefinidas": 5, "automaticas": 3, "numeros_errados": 1},
        {"variante": "direta", "categoria": "MARKETING", "enviados": 20,
         "respostas": 2, "botoes": 2},
    ]
    out = _metricas(sessao, monkeypatch, linhas)
    esperado = {"interessado": 15, "interesse": 9, "botao": 6, "parar": 2,
                "outro": 12, "disse_nao": 6, "sem_sinal": 5, "numero_errado": 1,
                "automatica": 3}
    assert out["total"]["respostas_por_tipo"] == esperado
    direta = next(v for v in out["variantes"] if v["id"] == "direta")
    assert direta["respostas_por_tipo"] == esperado
    assert esperado["interesse"] + esperado["botao"] == out["total"]["respostas"]


def test_placar_antigo_sem_botoes_nao_inventa_clique(sessao, monkeypatch):
    linhas = [{"variante": "direta", "categoria": "UTILITY", "enviados": 30,
               "respostas": 4}]
    tipos = _metricas(sessao, monkeypatch, linhas)["total"]["respostas_por_tipo"]
    assert (tipos["interesse"], tipos["botao"]) == (4, 0)


def test_amostra_pequena_marcada(sessao, monkeypatch):
    linhas = [{"variante": "direta", "categoria": "UTILITY", "enviados": 3,
               "respostas": 1}]
    out = _metricas(sessao, monkeypatch, linhas)
    direta = next(v for v in out["variantes"] if v["id"] == "direta")
    assert direta["amostra_pequena"] is True
    assert direta["sem_dado"] is False
    assert api.AMOSTRA_MINIMA == 20


def test_categoria_que_saiu_aparece_quebrada(sessao, monkeypatch):
    """Variante de marketing que caiu em UTILITY (cota fechada, template
    faltando) tem que ser visível — senão a tela diz que o teste de categoria
    está rodando quando não está."""
    linhas = [
        {"variante": "direta_marketing", "categoria": "MARKETING",
         "enviados": 20, "respostas": 2},
        {"variante": "direta_marketing", "categoria": "UTILITY",
         "enviados": 80, "respostas": 6},
    ]
    out = _metricas(sessao, monkeypatch, linhas)
    v = next(x for x in out["variantes"] if x["id"] == "direta_marketing")
    assert v["enviados"] == 100
    assert [c["categoria"] for c in v["por_categoria"]] == ["UTILITY", "MARKETING"]


def test_disparo_antigo_fica_fora_do_placar(sessao, monkeypatch):
    """'antes-do-teste' não é variante escolhível: somar no placar faria a Dona
    comparar com uma copy que ninguém pode mais rodar."""
    linhas = [
        {"variante": "direta", "categoria": "UTILITY", "enviados": 100,
         "respostas": 13},
        {"variante": "antes-do-teste", "categoria": "UTILITY", "enviados": 900,
         "respostas": 40},
    ]
    out = _metricas(sessao, monkeypatch, linhas)
    assert out["total"]["enviados"] == 100
    assert [x["variante"] for x in out["fora_do_teste"]] == ["antes-do-teste"]
    assert out["fora_do_teste"][0]["taxa"] == 4.4


def test_marco_encurta_a_janela(sessao, monkeypatch):
    """Recomeçou a medição ontem: 'últimos 30 dias' conta de ontem pra cá, e a
    tela diz desde quando."""
    class _Marco:
        criado_em = datetime.now(timezone.utc) - timedelta(days=1)
        motivo = "pesos novos"
        criado_por = "dona"

    out = _metricas(sessao, monkeypatch, [], marco=_Marco())
    assert out["medindo_desde"] is not None
    assert out["motivo_do_marco"] == "pesos novos"
    assert out["marco_por"] == "dona"
    desde = datetime.fromisoformat(out["desde"])
    assert (datetime.now(timezone.utc) - desde) < timedelta(days=2)


def test_marco_mais_velho_que_a_janela_nao_estica(sessao, monkeypatch):
    class _Marco:
        criado_em = datetime.now(timezone.utc) - timedelta(days=120)
        motivo = "antigo"
        criado_por = "dona"

    out = _metricas(sessao, monkeypatch, [], marco=_Marco())
    desde = datetime.fromisoformat(out["desde"])
    assert (datetime.now(timezone.utc) - desde) < timedelta(days=31)


def test_leva_peso_e_cota_pra_tela(sessao, monkeypatch):
    out = _metricas(sessao, monkeypatch, [])
    assert out["cota_marketing_pct"] == cc.COTA_MARKETING_PCT
    por_id = {v["id"]: v for v in out["variantes"]}
    assert por_id["direta"]["peso"] == por_id["direta"]["peso_padrao"]
    assert por_id["direta"]["peso_personalizado"] is False
    assert por_id["direta_marketing"]["categoria"] == "marketing"


def test_estoque_das_etapas(sessao, monkeypatch):
    out = _metricas(sessao, monkeypatch, [])
    assert out["estoque"] == {"fila": 120, "disparado": 40, "respondeu": 7}


def test_placar_caido_nao_derruba_a_tela(sessao, monkeypatch):
    from app.services import carrossel_corretor as cb

    def explode(*a, **k):
        raise RuntimeError("query morreu")

    monkeypatch.setattr(cb, "placar", explode)
    monkeypatch.setattr(api, "_ultimo_marco", lambda s: None)
    monkeypatch.setattr(api, "_estoque_carrossel",
                        lambda s: {"fila": 1, "disparado": 0, "respondeu": 0})
    out = api.calcular_metricas_remarketing(sessao, dias=30)
    assert out["total"]["sem_dado"] is True


def test_endpoint_e_admin_only():
    with pytest.raises(HTTPException) as e:
        api.metricas_remarketing(dias=30, x_painel_user="corretor")
    assert e.value.status_code in (401, 403)


def test_endpoint_recusa_janela_absurda():
    with pytest.raises(HTTPException) as e:
        api.metricas_remarketing(dias=5000, x_painel_user="dona")
    assert e.value.status_code == 422
