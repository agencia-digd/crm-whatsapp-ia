"""Placar honesto do Aprendiz: humano > juiz IA > pendente; juiz com teto; POST /acerto.

DB: SQLite in-memory (padrão de test_aprendiz_bateu.py). Zero prod, zero envio, zero claude real.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

import app.api.aprendiz as aprendiz
from app.llm.provider import TokenUsage
from app.services import juiz_aprendiz
from app.services.comparador_sombra import agregar_placar, veredito_final
from tests.test_aprendiz_bateu import client  # noqa: F401  (fixture com seed)

H_DONA = {"X-Painel-User": "dona"}
H_CORRETOR = {"X-Painel-User": "corretor"}


def test_veredito_final_precedencia():
    assert veredito_final(True, "nao_bateu", tem_real=True) == ("bateu", "humano")
    assert veredito_final(False, "bateu", tem_real=True) == ("nao_bateu", "humano")
    assert veredito_final(None, "parcial", tem_real=True) == ("parcial", "ia")
    assert veredito_final(None, None, tem_real=True) == ("pendente", None)
    assert veredito_final(None, None, tem_real=False) == ("sem_resposta_real", None)
    # humano vale mesmo sem resposta real
    assert veredito_final(True, None, tem_real=False) == ("bateu", "humano")


def test_placar_sem_avaliacao_nao_inventa_percentual(client):  # noqa: F811
    d = client.get("/aprendiz/bateu/placar", headers=H_DONA).json()
    # o trigrama continua lá (compatibilidade), mas o placar honesto está pendente
    assert d["bateu_pct"] == 67
    assert d["acertou_pct"] is None
    assert d["avaliados"] == 0
    assert d["pendentes_avaliacao"] == 3


def test_marcar_acerto_entra_no_placar(client):  # noqa: F811
    r = client.post("/aprendiz/acerto", json={"decisao_id": 102, "acerto": True}, headers=H_DONA)
    assert r.status_code == 200
    d = client.get("/aprendiz/bateu/placar", headers=H_DONA).json()
    assert d["final"]["bateu"] == 1
    assert d["acertou_pct"] == 100 and d["fonte_avaliacao"]["humano"] == 1
    # desfaz
    client.post("/aprendiz/acerto", json={"decisao_id": 102, "acerto": None}, headers=H_DONA)
    d = client.get("/aprendiz/bateu/placar", headers=H_DONA).json()
    assert d["avaliados"] == 0


def test_corretora_so_marca_as_dela(client):  # noqa: F811
    ok = client.post("/aprendiz/acerto", json={"decisao_id": 101, "acerto": False}, headers=H_CORRETOR)
    assert ok.status_code == 200
    outra = client.post("/aprendiz/acerto", json={"decisao_id": 105, "acerto": True}, headers=H_CORRETOR)
    assert outra.status_code == 403
    assert client.post("/aprendiz/acerto", json={"decisao_id": 999, "acerto": True},
                       headers=H_DONA).status_code == 404
    assert client.post("/aprendiz/acerto", json={"decisao_id": 101, "acerto": True}).status_code == 403


class _Juiz:
    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.chamadas = 0

    def complete(self, system, user, schema):
        self.chamadas += 1
        r = self.respostas.pop(0)
        if isinstance(r, Exception):
            raise r
        return json.dumps(r), TokenUsage(input_tokens=100, output_tokens=20)


def _pares(client):  # noqa: F811
    return aprendiz._carregar_pares(None, 30)


def test_juiz_grava_e_respeita_teto(client):  # noqa: F811
    prov = _Juiz([{"veredito": "bateu", "motivo": "mesma proposta de visita"},
                  {"veredito": "nao_bateu", "motivo": "ação diferente"},
                  {"veredito": "bateu", "motivo": "x"}])
    med = juiz_aprendiz.julgar_pendentes(_pares(client), aprendiz.SessionLocal, prov,
                                         modelo="haiku-teste", teto=2)
    assert prov.chamadas == 2 and med.julgados == 2 and med.pulados_por_teto == 1
    assert med.tokens_in == 200 and "teto 2" in med.linha()
    d = client.get("/aprendiz/bateu/placar", headers=H_DONA).json()
    assert d["fonte_avaliacao"]["ia"] == 2 and d["avaliados"] == 2
    with aprendiz.SessionLocal() as s:
        n = s.execute(text("SELECT count(*) FROM shadow_decisions WHERE juiz_modelo='haiku-teste'")).scalar()
    assert n == 2


def test_juiz_nao_rejulga_nem_sobrepoe_humano(client):  # noqa: F811
    client.post("/aprendiz/acerto", json={"decisao_id": 101, "acerto": True}, headers=H_DONA)
    prov = _Juiz([{"veredito": "bateu", "motivo": "a"}, {"veredito": "bateu", "motivo": "b"}])
    juiz_aprendiz.julgar_pendentes(_pares(client), aprendiz.SessionLocal, prov, modelo="m", teto=10)
    assert prov.chamadas == 2          # 101 tem marcação humana: só 102 e 105 vão ao juiz
    prov2 = _Juiz([])
    juiz_aprendiz.julgar_pendentes(_pares(client), aprendiz.SessionLocal, prov2, modelo="m", teto=10)
    assert prov2.chamadas == 0         # nada pendente


def test_juiz_para_na_cota_e_ignora_saida_invalida(client):  # noqa: F811
    prov = _Juiz([{"veredito": "talvez", "motivo": ""},
                  RuntimeError("exit 1 (permanente): You've hit your session limit")])
    med = juiz_aprendiz.julgar_pendentes(_pares(client), aprendiz.SessionLocal, prov, modelo="m", teto=10)
    assert med.falhas == 1 and med.parou_por_cota and med.julgados == 0
    assert prov.chamadas == 2          # parou: não insistiu na terceira


def test_agregar_placar_sem_pares():
    d = agregar_placar([])
    assert d["acertou_pct"] is None and d["avaliados"] == 0


def test_aprendizado_evolucao_mudancas_e_correcoes(client):  # noqa: F811
    client.post("/aprendiz/acerto", json={"decisao_id": 102, "acerto": False}, headers=H_DONA)
    r = client.get("/aprendiz/aprendizado", headers=H_DONA)
    assert r.status_code == 200
    d = r.json()
    assert d["evolucao"] and d["evolucao"][-1]["decisoes"] == 5
    assert [m["data"] for m in d["mudancas"]][0] == "2026-08-05"
    assert d["mudancas"][0]["efeito"]["depois_n"] == 4          # 4 rascunhos, todos pós-corte
    assert [c["decisao_id"] for c in d["correcoes"]] == [102]
    assert d["correcoes"][0]["fonte"] == "humano"
    assert d["correcoes_alimentam_prompt"] is False
    # corretora só enxerga o dela
    corretor = client.get("/aprendiz/aprendizado?corretora_id=1", headers=H_CORRETOR).json()
    assert corretor["corretora_id"] == 3 and corretor["evolucao"][-1]["decisoes"] == 4
    assert client.get("/aprendiz/aprendizado").status_code == 403
