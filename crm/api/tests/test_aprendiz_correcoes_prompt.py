"""Correções humanas voltam pro prompt do Aprendiz (flag APRENDIZ_CORRECOES_NO_PROMPT).

Trava principal: com a flag OFF (padrão) o prompt enviado à sombra e ao juiz é
IDÊNTICO, byte a byte, ao de antes. DB: SQLite in-memory (padrão de
test_aprendiz_bateu.py). Zero prod, zero envio, zero claude real.
"""
from __future__ import annotations

import json

import pytest

import app.api.aprendiz as aprendiz
from app.inspector import shadow
from app.llm.provider import TokenUsage
from app.services import aprendiz_correcoes as ac
from app.services import juiz_aprendiz
from tests.test_aprendiz_bateu import client  # noqa: F401  (fixture com seed)

H_DONA = {"X-Painel-User": "dona"}


class _Espiao:
    """Provider falso: guarda o system prompt recebido."""

    def __init__(self, resposta: dict):
        self.resposta = resposta
        self.systems: list[str] = []

    def complete(self, system, user, schema=None):
        self.systems.append(system)
        return json.dumps(self.resposta), TokenUsage(input_tokens=1, output_tokens=1)


_CONVERSA = [{"from": "lead", "text": "tem 2 quartos?", "at": "2026-10-21T10:00:00Z"}]


def _sombra(prov):
    return shadow.decidir_sombra("Pessoa3", 3, "Beto", "k2", "+5500000000000", _CONVERSA, provider=prov)


@pytest.fixture(autouse=True)
def _sem_flag(monkeypatch):
    monkeypatch.delenv("APRENDIZ_CORRECOES_NO_PROMPT", raising=False)
    monkeypatch.delenv("APRENDIZ_CORRECOES_DESDE", raising=False)


def test_flag_off_sombra_prompt_identico_byte_a_byte(client, monkeypatch):  # noqa: F811
    client.post("/aprendiz/acerto", json={"decisao_id": 102, "acerto": False}, headers=H_DONA)
    monkeypatch.setattr(ac, "ultimas_correcoes", lambda *a, **k: pytest.fail("não podia consultar o banco"))
    prov = _Espiao({"bucket": "rascunharia", "confianca": 60, "resposta_sugerida": "x", "motivo": "y"})
    assert _sombra(prov) is not None
    assert prov.systems[0].encode() == shadow._SYSTEM.encode()


def test_flag_off_juiz_prompt_identico_byte_a_byte(client):  # noqa: F811
    client.post("/aprendiz/acerto", json={"decisao_id": 102, "acerto": False}, headers=H_DONA)
    prov = _Espiao({"veredito": "bateu", "motivo": "ok"})
    juiz_aprendiz.julgar(prov, "oi", "rascunho", "real", corretora_id=3)
    assert prov.systems[0].encode() == juiz_aprendiz._SYSTEM.encode()
    assert ac.bloco_para_prompt(3, aprendiz.SessionLocal) == ""


def test_flag_on_entra_correcao_humana_nos_dois_prompts(client, monkeypatch):  # noqa: F811
    client.post("/aprendiz/acerto", json={"decisao_id": 102, "acerto": False}, headers=H_DONA)
    monkeypatch.setenv("APRENDIZ_CORRECOES_NO_PROMPT", "1")
    import app.db.session as sessao
    monkeypatch.setattr(sessao, "SessionLocal", aprendiz.SessionLocal)  # banco SQLite da fixture
    prov = _Espiao({"bucket": "rascunharia", "confianca": 60, "resposta_sugerida": "x", "motivo": "y"})
    _sombra(prov)
    s = prov.systems[0]
    assert s.startswith(shadow._SYSTEM) and "CORREÇÕES RECENTES" in s
    assert "Te mando as fotos do apartamento de 2 quartos!" in s  # rascunho 102, marcado errado
    assert "Oi Corretor" not in s                                         # 101 não foi marcado


def test_so_correcao_humana_e_teto_de_cinco(client):  # noqa: F811
    # 101 humano acertou (não entra), 102 humano errou (entra), 103 só juiz (não entra)
    client.post("/aprendiz/acerto", json={"decisao_id": 101, "acerto": True}, headers=H_DONA)
    client.post("/aprendiz/acerto", json={"decisao_id": 102, "acerto": False}, headers=H_DONA)
    assert ac.ultimas_correcoes(aprendiz.SessionLocal) == ["Te mando as fotos do apartamento de 2 quartos!"]
    with aprendiz.SessionLocal() as s:
        from sqlalchemy import text
        for i in range(10):
            s.execute(text(
                "INSERT INTO shadow_decisions (id, corretora_id, bucket, confianca, resposta_sugerida, acerto, avaliado_em, created_at) "
                "VALUES (:i, 3, 'rascunharia', 50, :r, 0, datetime('now'), datetime('now'))"),
                {"i": 900 + i, "r": f"Rascunho errado número {i} sobre visita"})
        s.commit()
    assert len(ac.ultimas_correcoes(aprendiz.SessionLocal, limite=99)) == 5


def test_limpar_tira_dado_pessoal():
    t = ac.limpar("Oi Beto, me liga em +5500000000000 ou contato@example.com, veja https://x.com/a", "Fulano")
    assert t == "Oi [nome], me liga em [número] ou [email], veja [link]"
    # ainda cheio de número depois da limpeza: descarta inteiro
    assert ac.limpar("a1 b2 c3 d4 e5 f6 g7 casa", None) is None
    assert ac.limpar("1 2 3 4 5 6 7 8 imóvel", None) == "[número] imóvel"
    assert ac.limpar("   ", None) is None
    assert len(ac.limpar("a" * 900, None)) <= 280


def test_falha_de_banco_vira_bloco_vazio(monkeypatch):
    monkeypatch.setenv("APRENDIZ_CORRECOES_NO_PROMPT", "1")
    def quebra(*a, **k):
        raise RuntimeError("banco fora")
    monkeypatch.setattr(ac, "ultimas_correcoes", quebra)
    assert ac.bloco_para_prompt(1) == ""


def test_endpoint_reflete_a_flag_e_mede_efeito(client, monkeypatch):  # noqa: F811
    client.post("/aprendiz/acerto", json={"decisao_id": 102, "acerto": False}, headers=H_DONA)
    d = client.get("/aprendiz/aprendizado", headers=H_DONA).json()
    assert d["correcoes_alimentam_prompt"] is False
    assert all(m["titulo"] != "Correções voltam pro prompt" for m in d["mudancas"])
    monkeypatch.setenv("APRENDIZ_CORRECOES_NO_PROMPT", "true")
    monkeypatch.setenv("APRENDIZ_CORRECOES_DESDE", "2000-01-01")
    d = client.get("/aprendiz/aprendizado", headers=H_DONA).json()
    assert d["correcoes_alimentam_prompt"] is True
    m = [x for x in d["mudancas"] if x["titulo"] == "Correções voltam pro prompt"][0]
    assert m["efeito"]["antes_n"] == 0 and m["efeito"]["depois_n"] == 1 and m["efeito"]["depois_pct"] == 0
