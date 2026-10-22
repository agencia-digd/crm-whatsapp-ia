"""Testes do receptor REAL do webhook de Lead Ads da Meta — serviço + endpoint.

Cobre o cano DIRETO Meta → copiloto (intake próprio, aposenta o Kommo):

  Serviço (processar_evento_leadgen, Graph mockado, intake_fn fake):
    - flag OFF → status 'desligado', intake NÃO chamado;
    - flag ON → intake chamado com origem='meta_leadgen', external_ref=leadgen_id
      e a origem rica (campaign_name/ad_name/form_id);
    - Graph falhou → status 'erro_graph', sem tocar no intake;
    - sem telefone válido → status 'sem_telefone';
    - dedup: 2 eventos mesmo leadgen_id → intake chamado 2x MAS com o mesmo
      external_ref (o intake dedup por telefone/external_ref — provado em
      test_lead_intake);

  Endpoint (GET+POST /intake/meta-webhook, TestClient):
    - GET verificação: token certo devolve challenge; token vazio/errado 403;
    - POST assinatura inválida → 403; assinatura válida com flag OFF → 200
      "desligado" sem processar; assinatura válida com flag ON → delega.

Zero prod, zero rede: fetcher e intake fakes; assinatura HMAC calculada local.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import patch

import pytest


# Página dos payloads de teste (fictícia). Tem que bater com `_VALUE["page_id"]`.
_PAGINA_TESTE = "123123123"


@pytest.fixture(autouse=True)
def _pagina_de_teste_configurada(monkeypatch):
    """Configura a página aceita = a página fictícia dos payloads.

    Até 26/09/2026 esta fixture ZERAVA `intake_meta_page_id`, porque vazio
    significava "aceita qualquer página". Na fase 0 do CRM único o desenho
    virou: página não configurada FECHA (`pagina_nao_configurada`), pra um
    .env sem a variável não botar lead de outro cliente na espinha da Acme.
    Os dois lados do filtro têm teste próprio no fim do arquivo.
    """
    from app.config import settings
    monkeypatch.setattr(settings, "intake_meta_page_id", _PAGINA_TESTE)


from app.config import settings
from app.services.meta_webhook import processar_evento_leadgen


# value do change leadgen (formato Meta)
_VALUE = {
    "leadgen_id": "000000000000000",
    "ad_id": "6060606060",
    "form_id": "7070707070",
    "page_id": "123123123",
    "created_time": 1753300000,
}

# Resposta rica da Graph API pro nó do leadgen
_GRAPH = {
    "id": "000000000000000",
    "created_time": "2026-07-21T23:03:30+0000",
    "ad_id": "6060606060",
    "ad_name": "Rizz Bairro A | Zona Sul | Lazer (D6)",
    "adset_id": "5050505050",
    "adset_name": "ZS 25-45 Interesse Imóvel",
    "campaign_id": "4040404040",
    "campaign_name": "Acme | Lead | Morada - Teste de Criativos (CBO)",
    "form_id": "7070707070",
    "field_data": [
        {"name": "qual_seu_whatsapp?", "values": ["+5500000000000"]},
        {"name": "full_name", "values": ["João da Silva"]},
        {"name": "phone_number", "values": ["+5500000000000"]},
        {"name": "email", "values": ["contato@example.com"]},
    ],
}


def _fetcher_ok(leadgen_id: str) -> dict:
    assert leadgen_id == _VALUE["leadgen_id"]
    return _GRAPH


# ── serviço: processar_evento_leadgen ────────────────────────────────────────

def test_flag_off_nao_chama_intake():
    chamadas: list[dict] = []
    with patch.object(settings, "lead_intake_ativo", False):
        res = processar_evento_leadgen(
            _VALUE,
            fetcher=_fetcher_ok,
            intake_fn=lambda **kw: chamadas.append(kw),
        )
    assert res["status"] == "desligado"
    assert chamadas == []


def test_flag_on_chama_intake_com_origem_rica():
    chamadas: list[dict] = []

    class _Res:
        lead_id = 42
        ja_existia = False

    def _intake(**kw):
        chamadas.append(kw)
        return _Res()

    with patch.object(settings, "lead_intake_ativo", True):
        res = processar_evento_leadgen(_VALUE, fetcher=_fetcher_ok, intake_fn=_intake)

    assert res["status"] == "ok"
    assert res["lead_id"] == 42
    assert len(chamadas) == 1
    kw = chamadas[0]
    assert kw["telefone"] == "+5500000000000"
    assert kw["nome"] == "João da Silva"
    assert kw["origem"] == "meta_leadgen"
    assert kw["external_ref"] == _VALUE["leadgen_id"]
    assert kw["origem_campanha"] == _GRAPH["campaign_name"]
    assert kw["origem_anuncio"] == _GRAPH["ad_name"]
    assert kw["origem_form"] == "7070707070"


def test_graph_falhou_status_erro_sem_intake():
    chamadas: list[dict] = []

    def _fetcher_erro(leadgen_id: str) -> dict:
        raise RuntimeError("Graph API 500")

    with patch.object(settings, "lead_intake_ativo", True):
        res = processar_evento_leadgen(
            _VALUE, fetcher=_fetcher_erro, intake_fn=lambda **kw: chamadas.append(kw)
        )
    assert res["status"] == "erro_graph"
    assert chamadas == []


def test_sem_telefone_valido_nao_chama_intake():
    chamadas: list[dict] = []
    graph_sem_tel = {
        **_GRAPH,
        "field_data": [
            {"name": "full_name", "values": ["Paula"]},
            {"name": "qual_seu_whatsapp?", "values": ["quero informações"]},
        ],
    }
    with patch.object(settings, "lead_intake_ativo", True):
        res = processar_evento_leadgen(
            _VALUE,
            fetcher=lambda _id: graph_sem_tel,
            intake_fn=lambda **kw: chamadas.append(kw),
        )
    assert res["status"] == "sem_telefone"
    assert chamadas == []


def test_sem_leadgen_id_ignora():
    res = processar_evento_leadgen({"ad_id": "1"}, fetcher=_fetcher_ok)
    assert res["status"] == "sem_leadgen_id"


def test_dedup_mesmo_leadgen_id_mesmo_external_ref():
    # O serviço não guarda estado; a dedup real está no intake (external_ref/
    # telefone). Aqui garantimos que 2 eventos do mesmo leadgen_id sempre mandam
    # o MESMO external_ref pro intake — o que faz a espinha convergir num lead.
    chamadas: list[dict] = []

    class _Res:
        lead_id = 1
        ja_existia = True

    with patch.object(settings, "lead_intake_ativo", True):
        for _ in range(2):
            processar_evento_leadgen(
                _VALUE,
                fetcher=_fetcher_ok,
                intake_fn=lambda **kw: (chamadas.append(kw), _Res())[1],
            )
    assert len(chamadas) == 2
    assert chamadas[0]["external_ref"] == chamadas[1]["external_ref"] == _VALUE["leadgen_id"]


# ── endpoint GET+POST /intake/meta-webhook ───────────────────────────────────

@pytest.fixture()
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.meta_webhook import router

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


def _assinar(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# GET verificação ------------------------------------------------------------

def test_get_verify_ok_devolve_challenge(client):
    with patch.object(settings, "intake_meta_verify_token", "verify-me"):
        resp = client.get(
            "/intake/meta-webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "verify-me",
                "hub.challenge": "1234567890",
            },
        )
    assert resp.status_code == 200
    assert resp.text == "1234567890"


def test_get_verify_token_errado_403(client):
    with patch.object(settings, "intake_meta_verify_token", "verify-me"):
        resp = client.get(
            "/intake/meta-webhook",
            params={"hub.mode": "subscribe", "hub.verify_token": "errado",
                    "hub.challenge": "x"},
        )
    assert resp.status_code == 403


def test_get_verify_fail_closed_sem_token_configurado(client):
    with patch.object(settings, "intake_meta_verify_token", ""):
        resp = client.get(
            "/intake/meta-webhook",
            params={"hub.mode": "subscribe", "hub.verify_token": "qualquer",
                    "hub.challenge": "x"},
        )
    assert resp.status_code == 403


# POST entrega ---------------------------------------------------------------

def test_post_assinatura_invalida_403(client):
    body = json.dumps(_BODY).encode()
    with patch.object(settings, "intake_meta_app_secret", "app-secret"):
        resp = client.post(
            "/intake/meta-webhook",
            content=body,
            headers={"X-Hub-Signature-256": "sha256=deadbeef"},
        )
    assert resp.status_code == 403


def test_post_fail_closed_sem_app_secret_403(client):
    body = json.dumps(_BODY).encode()
    with patch.object(settings, "intake_meta_app_secret", ""):
        resp = client.post(
            "/intake/meta-webhook",
            content=body,
            headers={"X-Hub-Signature-256": _assinar(body, "qualquer")},
        )
    assert resp.status_code == 403


def test_post_flag_off_200_sem_processar(client):
    body = json.dumps(_BODY).encode()
    with patch.object(settings, "intake_meta_app_secret", "app-secret"), \
         patch.object(settings, "lead_intake_ativo", False):
        resp = client.post(
            "/intake/meta-webhook",
            content=body,
            headers={"X-Hub-Signature-256": _assinar(body, "app-secret")},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "desligado"
    assert resp.json()["processados"] == 0


def test_post_flag_on_delega_pro_servico(client):
    body = json.dumps(_BODY).encode()
    fake = {"leadgen_id": _VALUE["leadgen_id"], "status": "ok", "lead_id": 7}
    with patch.object(settings, "intake_meta_app_secret", "app-secret"), \
         patch.object(settings, "lead_intake_ativo", True), \
         patch("app.api.meta_webhook.processar_evento_leadgen", return_value=fake) as m:
        resp = client.post(
            "/intake/meta-webhook",
            content=body,
            headers={"X-Hub-Signature-256": _assinar(body, "app-secret")},
        )
    assert resp.status_code == 200
    body_json = resp.json()
    assert body_json["processados"] == 1
    assert body_json["resultados"][0]["status"] == "ok"
    m.assert_called_once_with(_VALUE)


def test_post_ignora_change_nao_leadgen(client):
    payload = {"entry": [{"changes": [{"field": "feed", "value": {}}]}]}
    body = json.dumps(payload).encode()
    with patch.object(settings, "intake_meta_app_secret", "app-secret"), \
         patch.object(settings, "lead_intake_ativo", True):
        resp = client.post(
            "/intake/meta-webhook",
            content=body,
            headers={"X-Hub-Signature-256": _assinar(body, "app-secret")},
        )
    assert resp.status_code == 200
    assert resp.json()["processados"] == 0


def test_filtro_por_pagina_ignora_pagina_estranha(monkeypatch):
    from unittest.mock import patch as _patch

    from app.config import settings
    from app.services.meta_webhook import processar_evento_leadgen

    monkeypatch.setattr(settings, "intake_meta_page_id", "000000000000000")
    chamado = {"n": 0}

    def _intake(**kw):
        chamado["n"] += 1

    with _patch.object(settings, "lead_intake_ativo", True):
        res = processar_evento_leadgen(
            {"leadgen_id": "999", "page_id": "123123123"},
            fetcher=lambda _id: {"field_data": []},
            intake_fn=_intake,
        )
    assert res["status"] == "outra_pagina"
    assert chamado["n"] == 0


def test_pagina_nao_configurada_fecha_a_porta(monkeypatch):
    """Sem INTAKE_META_PAGE_ID o receptor não aceita lead de ninguém (26/09):
    nem busca na Graph, nem chama o intake."""
    from unittest.mock import patch as _patch

    from app.config import settings
    from app.services.meta_webhook import processar_evento_leadgen

    monkeypatch.setattr(settings, "intake_meta_page_id", "")
    chamado = {"fetch": 0, "intake": 0}

    def _fetch(_id):
        chamado["fetch"] += 1
        return {"field_data": []}

    def _intake(**kw):
        chamado["intake"] += 1

    with _patch.object(settings, "lead_intake_ativo", True):
        res = processar_evento_leadgen(
            {"leadgen_id": "998", "page_id": _PAGINA_TESTE},
            fetcher=_fetch,
            intake_fn=_intake,
        )
    assert res["status"] == "pagina_nao_configurada"
    assert chamado == {"fetch": 0, "intake": 0}
