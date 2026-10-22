"""Testes do receptor Meta leadgen em MODO SOMBRA (B6) — serviço + endpoint.

Cobre:
  - extração do payload Meta (parse_field_data): nome full_name / first+last,
    telefone preferindo `qual_seu_whatsapp?` válido com fallback pro
    `phone_number` do Facebook, rejeição de texto livre;
  - sombra SEMPRE grava `meta_leads_sombra` (com campanha da Graph) e, com a
    flag `lead_intake_ativo` OFF (default), NÃO chama o intake (processado=False);
  - dedup por leadgen_id (2 webhooks → 1 linha);
  - Graph falhou → linha gravada mesmo assim (raw com erro);
  - flag ON → intake_fn chamado e processado=True;
  - endpoint POST /intake/meta-leadgen/{secret}: fail-closed (secret vazio /
    errado = 401) e delegação pro serviço.

DB isolado: SQLite in-memory (mesmo padrão de test_lead_intake). Zero prod,
zero rede — fetcher e intake fakes injetados.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db.models import Base, MetaLeadSombra
from app.services.meta_leadgen import parse_field_data, processar_leadgen_value


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "INTEGER"


@pytest.fixture()
def flag_intake_off():
    """Garante `lead_intake_ativo=False` durante o teste.

    Sem isso, os testes que chamam `processar_leadgen_value` SEM `intake_fn`
    caem no intake REAL quando o `.env` de produção tem LEAD_INTAKE_ATIVO=true —
    ou seja, o teste escreveria lead de verdade. O conftest também pina a env em
    import-time; esta fixture é a garantia local e explícita.
    """
    with patch.object(settings, "lead_intake_ativo", False):
        yield


@pytest.fixture(autouse=True)
def _pagina_de_teste_configurada(monkeypatch):
    """Página aceita = a página fictícia de `_VALUE`.

    Desde 26/09/2026 (fase 0 do CRM único) `processar_leadgen_value` filtra
    por página ANTES de gravar a sombra: página não configurada devolve
    `pagina_nao_configurada` e a de outro cliente, `outra_pagina`. O conftest
    pina INTAKE_META_PAGE_ID vazio, então sem isto todo cenário parava na porta.
    """
    monkeypatch.setattr(settings, "intake_meta_page_id", "123123123")


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[MetaLeadSombra.__table__])
    maker = sessionmaker(bind=engine)
    # UMA connection compartilhada: sqlite:// in-memory some a cada connect novo
    conn = engine.connect()
    yield lambda: maker(bind=conn)
    conn.close()


# Payload REAL de webhook leadgen (formato Meta) — value do change
_VALUE = {
    "leadgen_id": "000000000000000",
    "ad_id": "6060606060",
    "form_id": "7070707070",
    "page_id": "123123123",
    "created_time": 1753300000,
}

# Resposta da Graph API pro nó do leadgen (formato validado 24/07 com lead real)
_GRAPH = {
    "id": "000000000000000",
    "created_time": "2026-07-21T23:03:30+0000",
    "ad_id": "6060606060",
    "ad_name": "Rizz Bairro A | Zona Sul | Lazer (D6)",
    "campaign_name": "Acme | Lead | Morada - Teste de Criativos (CBO)",
    "form_id": "7070707070",
    "field_data": [
        {"name": "qual_seu_whatsapp?", "values": ["+5500000000000"]},
        {"name": "full_name", "values": ["João da Silva"]},
        {"name": "phone_number", "values": ["+5500000000000"]},
    ],
}


def _fetcher_ok(leadgen_id: str) -> dict:
    assert leadgen_id == _VALUE["leadgen_id"]
    return _GRAPH


# ── parse_field_data ─────────────────────────────────────────────────────────

def test_parse_prefere_whatsapp_customizado_valido():
    campos = parse_field_data(_GRAPH["field_data"])
    assert campos["nome"] == "João da Silva"
    assert campos["telefone"] == "+5500000000000"


def test_parse_fallback_phone_number_quando_customizado_e_texto_livre():
    field_data = [
        {"name": "qual_seu_whatsapp?", "values": ["Gostaria de saber os valores"]},
        {"name": "full_name", "values": ["Paula"]},
        {"name": "phone_number", "values": ["+5500000000000"]},
    ]
    campos = parse_field_data(field_data)
    assert campos["telefone"] == "+5500000000000"


def test_parse_nome_first_last_quando_sem_full_name():
    field_data = [
        {"name": "first_name", "values": ["Pessoa"]},
        {"name": "last_name", "values": ["Souza"]},
        {"name": "phone_number", "values": ["+5500000000000"]},
    ]
    campos = parse_field_data(field_data)
    assert campos["nome"] == "Pessoa Souza"


def test_parse_sem_telefone_valido_nao_inventa():
    campos = parse_field_data(
        [{"name": "qual_seu_whatsapp?", "values": ["quero informações"]}]
    )
    assert "telefone" not in campos


# ── processar_leadgen_value (sombra) ─────────────────────────────────────────

def test_sombra_grava_linha_sem_chamar_intake_com_flag_off(session_factory):
    chamadas: list[dict] = []

    # Flag patchada explicitamente: o `.env` de produção define
    # LEAD_INTAKE_ATIVO e este teste afirma o comportamento com ela OFF, não o
    # estado do servidor. Com a flag ligada no .env, o assert `chamadas == []`
    # falhava — e o intake era chamado de verdade.
    with patch.object(settings, "lead_intake_ativo", False):
        resultado = processar_leadgen_value(
            _VALUE,
            fetcher=_fetcher_ok,
            session_factory=session_factory,
            intake_fn=lambda **kw: chamadas.append(kw),
        )

    assert resultado["status"] == "gravado"
    assert resultado["intake"] == "sombra"
    assert resultado["processado"] is False
    assert chamadas == []  # flag OFF (default) → intake NÃO é tocado

    s = session_factory()
    linha = s.query(MetaLeadSombra).one()
    assert linha.leadgen_id == _VALUE["leadgen_id"]
    assert linha.nome == "João da Silva"
    assert linha.telefone == "+5500000000000"
    assert linha.campanha == "Acme | Lead | Morada - Teste de Criativos (CBO)"
    assert linha.ad_id == "6060606060"
    assert linha.form_id == "7070707070"
    assert linha.processado is False
    assert linha.raw["webhook"]["leadgen_id"] == _VALUE["leadgen_id"]
    assert linha.raw["graph"]["campaign_name"] == linha.campanha
    s.close()


def test_sombra_dedup_por_leadgen_id(session_factory, flag_intake_off):
    processar_leadgen_value(_VALUE, fetcher=_fetcher_ok, session_factory=session_factory)
    resultado = processar_leadgen_value(
        _VALUE, fetcher=_fetcher_ok, session_factory=session_factory
    )
    assert resultado["status"] == "duplicado"
    s = session_factory()
    assert s.query(MetaLeadSombra).count() == 1
    s.close()


def test_sombra_grava_mesmo_com_graph_fora(session_factory, flag_intake_off):
    def _fetcher_erro(leadgen_id: str) -> dict:
        raise RuntimeError("Graph API 500")

    resultado = processar_leadgen_value(
        _VALUE, fetcher=_fetcher_erro, session_factory=session_factory
    )
    assert resultado["status"] == "gravado"

    s = session_factory()
    linha = s.query(MetaLeadSombra).one()
    assert linha.telefone is None  # sem field_data não tem como
    assert linha.ad_id == "6060606060"  # veio do value do webhook
    assert "erro_graph" in linha.raw
    s.close()


def test_sombra_sem_leadgen_id_ignora(session_factory, flag_intake_off):
    resultado = processar_leadgen_value(
        {"ad_id": "1"}, fetcher=_fetcher_ok, session_factory=session_factory
    )
    assert resultado["status"] == "sem_leadgen_id"


def test_flag_on_chama_intake_e_marca_processado(session_factory):
    chamadas: list[dict] = []

    def _intake_fake(**kw):
        chamadas.append(kw)
        return {"lead_id": 1}  # truthy = intake aceitou

    with patch.object(settings, "lead_intake_ativo", True):
        resultado = processar_leadgen_value(
            _VALUE,
            fetcher=_fetcher_ok,
            session_factory=session_factory,
            intake_fn=_intake_fake,
        )

    assert resultado["intake"] == "ok"
    assert resultado["processado"] is True
    assert len(chamadas) == 1
    assert chamadas[0]["telefone"] == "+5500000000000"
    assert chamadas[0]["origem"] == "meta_leadgen"
    assert chamadas[0]["external_ref"] == _VALUE["leadgen_id"]

    s = session_factory()
    assert s.query(MetaLeadSombra).one().processado is True
    s.close()


def test_flag_on_intake_falhou_sombra_fica(session_factory):
    def _intake_boom(**kw):
        raise ValueError("telefone inválido pro intake")

    with patch.object(settings, "lead_intake_ativo", True):
        resultado = processar_leadgen_value(
            _VALUE,
            fetcher=_fetcher_ok,
            session_factory=session_factory,
            intake_fn=_intake_boom,
        )

    assert resultado["intake"] == "erro"
    assert resultado["processado"] is False
    s = session_factory()
    assert s.query(MetaLeadSombra).count() == 1  # auditoria fica mesmo assim
    s.close()


def test_pagina_nao_configurada_nao_grava_sombra(session_factory, monkeypatch):
    """Sem página configurada o caminho para ANTES do banco (26/09)."""
    monkeypatch.setattr(settings, "intake_meta_page_id", "")
    resultado = processar_leadgen_value(
        _VALUE, fetcher=_fetcher_ok, session_factory=session_factory
    )
    assert resultado["status"] == "pagina_nao_configurada"
    s = session_factory()
    assert s.query(MetaLeadSombra).count() == 0
    s.close()


def test_pagina_de_outro_cliente_nao_grava_sombra(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "intake_meta_page_id", "000000000000000")
    resultado = processar_leadgen_value(
        _VALUE, fetcher=_fetcher_ok, session_factory=session_factory
    )
    assert resultado["status"] == "outra_pagina"
    s = session_factory()
    assert s.query(MetaLeadSombra).count() == 0
    s.close()


# ── endpoint POST /intake/meta-leadgen/{secret} ──────────────────────────────

@pytest.fixture()
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.meta_leadgen import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


_BODY = {
    "object": "page",
    "entry": [
        {
            "id": "123123123",
            "time": 1753300000,
            "changes": [{"field": "leadgen", "value": _VALUE}],
        }
    ],
}


def test_endpoint_fail_closed_sem_secret_configurado(client):
    with patch.object(settings, "meta_leadgen_secret", ""):
        resp = client.post("/intake/meta-leadgen/qualquer", json=_BODY)
    assert resp.status_code == 401


def test_endpoint_secret_errado_401(client):
    with patch.object(settings, "meta_leadgen_secret", "s3gr3do"):
        resp = client.post("/intake/meta-leadgen/errado", json=_BODY)
    assert resp.status_code == 401


def test_endpoint_ok_delega_pro_servico(client):
    fake = {"leadgen_id": _VALUE["leadgen_id"], "status": "gravado",
            "intake": "sombra", "processado": False}
    with patch.object(settings, "meta_leadgen_secret", "s3gr3do"), \
         patch("app.api.meta_leadgen.processar_leadgen_value", return_value=fake) as m:
        resp = client.post("/intake/meta-leadgen/s3gr3do", json=_BODY)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["processados"] == 1
    assert body["resultados"][0]["status"] == "gravado"
    m.assert_called_once_with(_VALUE)


def test_endpoint_ignora_change_que_nao_e_leadgen(client):
    body = {"entry": [{"changes": [{"field": "feed", "value": {}}]}]}
    with patch.object(settings, "meta_leadgen_secret", "s3gr3do"):
        resp = client.post("/intake/meta-leadgen/s3gr3do", json=body)
    assert resp.status_code == 200
    assert resp.json()["processados"] == 0
