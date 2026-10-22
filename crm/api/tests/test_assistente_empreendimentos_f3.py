"""Testes F3 — refinos do handler de catálogo da Assistente Copilota (_empreendimentos).

100% mockados: httpx stubado com a fixture real do dash :8014 (capturada 11/07/2026)
ou payloads sintéticos. Nada de rede, nada de LLM, nada de banco.

Cobre: normalização de teto, tipo robusto (plural/caixa/sobrado/lote), quartos em
faixa ("2 ou 3" × bruto "2-3"), filtro de entrega (pronto/lançamento/ano), relax de
preço +10%, alternativas quando 0, selo de frescor (at/stale defensivo) e a regra
inviolável de nunca vazar link de Drive pro contexto do LLM.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import app.api.assistente_copilota as bc

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/empreendimentos_payload.json").read_text()
)


class _Resp:
    def __init__(self, data: Any) -> None:
        self._d = data

    def json(self) -> Any:
        return self._d


class _StubClient:
    """Substitui httpx.Client; devolve o payload configurado em _StubClient.payload."""

    payload: Any = None
    raise_exc: bool = False

    def __init__(self, *a: Any, **k: Any) -> None:
        pass

    def __enter__(self) -> "_StubClient":
        return self

    def __exit__(self, *a: Any) -> bool:
        return False

    def get(self, *a: Any, **k: Any) -> _Resp:
        if _StubClient.raise_exc:
            raise RuntimeError("dash fora do ar")
        return _Resp(_StubClient.payload)


@pytest.fixture()
def catalogo():
    """httpx stubado com a fixture real. yield permite trocar o payload no teste."""
    _StubClient.payload = FIXTURE
    _StubClient.raise_exc = False
    with patch.object(bc.httpx, "Client", _StubClient):
        yield _StubClient


# ─────────────────────────── helpers puros ───────────────────────────

def test_norm_txt_acentos_e_jd():
    assert bc._norm_txt("AlÉM Ponte") == "alem ponte"
    assert bc._norm_txt("Jd Europa") == "jardim europa"
    assert bc._norm_txt("JD. Iguatemi") == "jardim iguatemi"
    assert bc._norm_txt(None) == ""


@pytest.mark.parametrize("tc,alvo", [
    ("apto", "APTO"), ("APARTAMENTOS", "APTO"), ("apê", "APTO"), ("ap", "APTO"),
    ("casa", "CASA"), ("casas", "CASA"), ("sobrado", "CASA"), ("Sobrados", "CASA"),
    ("terreno", "TERR"), ("lote", "TERR"), ("loteamento", "TERR"),
    ("", None), (None, None), ("cobertura", None),
])
def test_tipo_alvo(tc, alvo):
    assert bc._emp_tipo_alvo(tc) == alvo


@pytest.mark.parametrize("q,esperado", [
    (2, {2}), ("2", {2}), ("2 ou 3", {2, 3}), ("2 e 3", {2, 3}),
    ("2-3", {2, 3}), ("2 a 4", {2, 3, 4}), (None, None), ("", None), ("muitos", None),
])
def test_quartos_alvo(q, esperado):
    assert bc._emp_quartos_alvo(q) == esperado


def test_quartos_item_faixa_e_norm():
    assert bc._emp_quartos_item({"quartos": "2-3"}) == {2, 3}
    assert bc._emp_quartos_item({"quartos": "1-3"}) == {1, 2, 3}
    assert bc._emp_quartos_item({"quartos": "2"}) == {2}
    assert bc._emp_quartos_item({"quartos": "", "quartos_norm": 2}) == {2}
    assert bc._emp_quartos_item({"quartos": "", "quartos_norm": None}) is None


@pytest.mark.parametrize("entrega,bucket,fmt,ok", [
    ("pronto", "pronto", "Pronto", True),
    ("pronto pra morar", "data", "12/27", False),
    ("lancamento", "lancamento", "", True),
    ("na planta", "data", "12/27", False),
    ("2027", "data", "01/27", True),
    ("2027", "data", "2027", True),
    ("27", "data", "AGO/27", True),
    ("2027", "data", "12/2028", False),
    ("", "data", "12/28", True),          # sem critério → não filtra
    ("sei la", "data", "12/28", True),    # critério ininteligível → não filtra
])
def test_entrega_ok(entrega, bucket, fmt, ok):
    e = {"entrega_bucket": bucket, "entrega_fmt": fmt}
    assert bc._emp_entrega_ok(e, entrega) is ok


# ─────────────────────────── _empreendimentos (fixture real) ───────────────────────────

def test_filtro_basico_zona_norte_teto_abreviado(catalogo):
    out = bc._empreendimentos({"tipo": "apto", "quartos": 2, "bairro": "zona norte", "preco_max": 300})
    itens = out["itens"]
    assert itens and len(itens) <= 8
    assert all(e["tipo"] == "APTO" and e["zona"] == "Zona Norte" and e["preco_num"] <= 300000
               for e in itens)


def test_bairro_sem_acento_e_jd(catalogo):
    # planilha grafa "AlÉM Ponte" e "Jd Europa"
    assert bc._empreendimentos({"tipo": "apto", "bairro": "alem ponte"})["itens"]
    nomes = [e["empreendimento"] for e in
             bc._empreendimentos({"tipo": "casa", "bairro": "jardim europa"})["itens"]]
    assert "Sobrados JD Europa" in nomes


def test_quartos_faixa_2_ou_3_inclui_item_faixa(catalogo):
    out = bc._empreendimentos({"tipo": "apartamento", "quartos": "2 ou 3", "bairro": "centro"})
    assert "IPA" in [e["empreendimento"] for e in out["itens"]]  # bruto '1-3' cobre 2 e 3


def test_quartos_explicito_vem_antes_de_desconhecido(catalogo):
    out = bc._empreendimentos({"tipo": "apto", "quartos": 3})
    itens = out["itens"]
    assert itens
    assert bc._emp_quartos_item(itens[0]) and 3 in bc._emp_quartos_item(itens[0])


def test_entrega_pronto(catalogo):
    out = bc._empreendimentos({"entrega": "pronto"})
    assert out["itens"] and all(e["entrega_bucket"] == "pronto" for e in out["itens"])


def test_relax_preco_10pct_com_aviso(catalogo):
    # não existe apto <=180k; mais barato é 184k (dentro de teto+10% = 198k)
    out = bc._empreendimentos({"tipo": "apto", "preco_max": 180000})
    assert out["itens"]
    assert all(e["preco_num"] <= 198000 for e in out["itens"])
    assert "10%" in out["aviso"]


def test_alternativas_quando_zero_mesmo_relaxado(catalogo):
    out = bc._empreendimentos({"tipo": "terreno", "preco_max": 100000})
    itens = out["itens"]
    assert itens and all(e["tipo"] == "TERRENO" for e in itens)
    assert itens[0]["preco_num"] == min(i["preco_num"] for i in itens)  # mais barato primeiro
    assert "alternativas" in out["aviso"]


def test_teto_exige_preco_conhecido(catalogo):
    out = bc._empreendimentos({"tipo": "apto", "preco_max": 200000})
    assert out["itens"] and all(e.get("preco_num") for e in out["itens"])


def test_drive_nunca_vaza_pro_contexto(catalogo):
    out = bc._empreendimentos({})
    assert out["itens"]
    assert all("drive" not in e for e in out["itens"])
    assert any(e.get("tem_material") for e in out["itens"])  # sinal 'tem material' fica


def test_catalogo_fora_do_ar_degrada_limpo(catalogo):
    _StubClient.raise_exc = True
    out = bc._empreendimentos({"tipo": "apto"})
    assert out["itens"] == [] and out["erro"]


# ─────────────────────────── frescor (at / stale — DEFENSIVO) ───────────────────────────

def _payload_sintetico(**extra: Any) -> dict:
    base = {"itens": [{"empreendimento": "X", "tipo": "APTO", "bairro": "Centro",
                       "zona": "Central", "quartos": "2", "quartos_norm": 2,
                       "preco_num": 200000, "entrega_bucket": "pronto",
                       "entrega_fmt": "Pronto", "drive": "http://drive/x"}]}
    base.update(extra)
    return base


def test_frescor_com_campo_at(catalogo):
    from datetime import datetime, timedelta, timezone
    at = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    _StubClient.payload = _payload_sintetico(at=at)
    out = bc._empreendimentos({})
    assert out["frescor"] == "catálogo de 3h atrás"
    assert "atencao" not in out


def test_frescor_fallback_atualizado_em(catalogo):
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat().replace("+00:00", "Z")
    _StubClient.payload = _payload_sintetico(atualizado_em=ts)
    out = bc._empreendimentos({})
    assert out["frescor"] == "catálogo de menos de 1h atrás"


def test_stale_gera_atencao(catalogo):
    _StubClient.payload = _payload_sintetico(stale=True)
    out = bc._empreendimentos({})
    assert "desatualizados" in out["atencao"]


def test_sem_at_sem_stale_degrada_limpo(catalogo):
    _StubClient.payload = _payload_sintetico()
    out = bc._empreendimentos({})
    assert "frescor" not in out and "atencao" not in out and out["itens"]


def test_at_invalido_nao_quebra(catalogo):
    _StubClient.payload = _payload_sintetico(at="ontem de tarde", stale=None)
    out = bc._empreendimentos({})
    assert out["itens"] and "frescor" not in out and "atencao" not in out
