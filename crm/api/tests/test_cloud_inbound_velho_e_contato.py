"""Duas coisas que o conserto do evento preso (05/10/2026) trouxe pro copiloto.

1. CARTÃO DE CONTATO. Mensagem do tipo `contacts` vem como lista; o
   `.get("caption")` em cima dela estourava e as portas devolviam 500. A
   mensagem não era gravada e o evento ficava preso no api-disparos, que a
   partir daí devolvia 503 pra todo lote da Meta que o trouxesse de novo.
   8 dos 34 presos de 28/09 a 05/10 eram cartões (Pessoa11, Pessoa12, Pessoa10, Corretor).

2. MENSAGEM VELHA. Destravado o caminho, a Meta reentrega o que ficou refém,
   com horas de atraso. O api-disparos marca `X-Cloud-Velho: 1` no que passou
   de 30 min. Aqui a mensagem é gravada, o card anda e o opt-out vale — mas
   nada que FALA com o contato roda: nem a Pessoa, nem a resposta da régua do
   proprietário, nem a bridge da Assistente.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import cloud_corretora, cloud_assistente
from app.api import cloud_corretora_a as corretor
from app.config import settings
from app.main import app
from app.services import cloud_lines

SECRET = "segredo-de-teste-velho"
LEAD = "+5500000000000"

CARTAO = {"from": LEAD, "id": "wamid.CARTAO", "timestamp": "1790638734",
          "type": "contacts",
          "contacts": [{"name": {"formatted_name": "Fulano Corretor", "first_name": "Fulano"},
                        "phones": [{"phone": "+5500000000000", "wa_id": "+5500000000000",
                                    "type": "CELL"}]}]}


@pytest.fixture(autouse=True)
def _linhas(monkeypatch):
    linhas = {
        "000000000000000": ("corretor", "corretora"),
        "000000000000000": ("corretor", "corretora"),
    }
    reg = {pid: cloud_lines.CloudLine(phone_id=pid, slug=slug, papel=papel,
                                      env_file="/etc/agencia/review.env",
                                      token_var="META_REVIEW_TOKEN")
           for pid, (slug, papel) in linhas.items()}
    monkeypatch.setattr(cloud_lines, "_LINES", reg)
    monkeypatch.setattr(cloud_lines, "_BY_SLUG", {v[0]: k for k, v in linhas.items()})
    monkeypatch.setattr(settings, "cloud_webhook_secret", SECRET)
    yield


@pytest.fixture()
def gravadas(monkeypatch):
    """Tudo que a porta mandou gravar, sem banco."""
    linhas: list[dict[str, Any]] = []

    def fake(**kw):
        linhas.append(kw)
        return True

    monkeypatch.setattr(corretor, "gravar", fake)
    monkeypatch.setattr(cloud_assistente, "gravar", fake)
    return linhas


@pytest.fixture()
def ganchos(monkeypatch):
    """Registra cada gancho do inbound que rodou, sem executar nenhum."""
    rodou: list[str] = []

    def marca(nome, retorno=None):
        def _f(*a, **k):
            rodou.append(nome)
            return retorno
        return _f

    monkeypatch.setattr(corretor, "tratar_resposta_disparo", marca("disparo"))
    monkeypatch.setattr(corretor, "_tratar_resposta_cadencia_inbound", marca("cadencia"))
    monkeypatch.setattr(corretor, "_tratar_saida_remarketing", marca("saida_remarketing"))
    monkeypatch.setattr(corretor, "tratar_intencao_indicacao", marca("indicacao"))
    monkeypatch.setattr(corretor, "_tratar_resposta_regua_proprietario", marca("regua_proprietario"))
    monkeypatch.setattr(cloud_corretora.cb, "processar_inbound", marca("carrossel", "respondeu"))
    monkeypatch.setattr(cloud_corretora.atendimento_corretor, "atender", marca("pessoa"))
    monkeypatch.setattr(cloud_corretora.atendimento_corretor, "marcar_opt_out", marca("opt_out"))
    from app.services import saida_remarketing
    monkeypatch.setattr(saida_remarketing, "processar_inbound", marca("saida_remarketing"))
    return rodou


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _texto(corpo: str, wamid: str = "wamid.T1") -> dict:
    return {"from": LEAD, "id": wamid, "timestamp": "1791225000", "type": "text",
            "text": {"body": corpo}}


def _post(client, caminho: str, msg: dict, velho: bool = False):
    headers = {"X-Cloud-Secret": SECRET}
    if velho:
        headers["X-Cloud-Velho"] = "1"
    return client.post(caminho, headers=headers, json={
        "messages": [msg], "contacts": [{"wa_id": LEAD, "profile": {"name": "Lead"}}]})


# ─── cartão de contato ───────────────────────────────────────────────────────
def test_texto_de_midia_le_cartao_legenda_e_tipo_desconhecido():
    assert corretor.texto_de_midia(CARTAO, "contacts") == "[contato] Fulano Corretor +5500000000000"
    assert corretor.texto_de_midia({"image": {"caption": "planta"}}, "image") == "planta"
    assert corretor.texto_de_midia({"image": {"id": "1"}}, "image") == "[image]"
    assert corretor.texto_de_midia({"contacts": []}, "contacts") == "[contacts]"
    assert corretor.texto_de_midia({"type": "unsupported"}, "unsupported") == "[unsupported]"
    assert corretor.texto_de_midia({"location": ["x"]}, "location") == "[location]"


def test_cartao_na_linha_de_corretora_e_gravado_em_vez_de_dar_500(client, gravadas, ganchos):
    r = _post(client, "/cloud/corretora/corretor/inbound", CARTAO)
    assert r.status_code == 200, r.text
    assert gravadas[0]["tipo"] == "contacts"
    assert gravadas[0]["texto"] == "[contato] Fulano Corretor +5500000000000"


def test_cartao_na_linha_da_corretor_e_gravado_em_vez_de_dar_500(client, gravadas, ganchos):
    r = _post(client, "/cloud/inbound", CARTAO)
    assert r.status_code == 200, r.text
    assert gravadas[0]["texto"].startswith("[contato] Fulano")


def test_cartao_mandado_pelo_celular_da_corretora_e_gravado(client, gravadas):
    eco = {**CARTAO, "from": "+5500000000000", "to": LEAD, "id": "wamid.ECO"}
    r = client.post("/cloud/corretora/corretor/echo", headers={"X-Cloud-Secret": SECRET},
                    json={"echoes": [eco]})
    assert r.status_code == 200, r.text
    assert gravadas[0]["from_corretora"] is True
    assert gravadas[0]["texto"].startswith("[contato] Fulano")


# ─── mensagem velha ──────────────────────────────────────────────────────────
def test_linha_da_corretor_mensagem_nova_a_pessoa_atende(client, gravadas, ganchos):
    assert _post(client, "/cloud/corretora/corretor/inbound", _texto("oi")).status_code == 200
    assert ganchos == ["carrossel", "pessoa"]


def test_linha_da_corretor_mensagem_velha_move_o_card_e_a_pessoa_nao_fala(client, gravadas, ganchos):
    """O "não quero" que ficou refém de um 503: entra na thread e o carrossel
    classifica (o card vai pra Venda perdida), sem a Pessoa responder horas depois."""
    r = _post(client, "/cloud/corretora/corretor/inbound", _texto("não quero"), velho=True)
    assert r.status_code == 200
    assert [g["texto"] for g in gravadas] == ["não quero"]
    assert ganchos == ["carrossel"]


def test_linha_de_corretora_mensagem_velha_ainda_para_cadencia_e_vira_optout(
        client, gravadas, ganchos):
    r = _post(client, "/cloud/corretora/corretor/inbound",
              _texto("não quero mais receber mensagens"), velho=True)
    assert r.status_code == 200
    assert "cadencia" in ganchos and "opt_out" in ganchos and "pessoa" not in ganchos


def test_linha_da_corretor_mensagem_velha_registra_mas_nao_responde_o_proprietario(
        client, gravadas, ganchos):
    assert _post(client, "/cloud/inbound", _texto("Já vendi"), velho=True).status_code == 200
    assert ganchos == ["disparo", "cadencia", "saida_remarketing", "indicacao"]


def test_linha_da_corretor_mensagem_nova_segue_com_todos_os_ganchos(client, gravadas, ganchos):
    assert _post(client, "/cloud/inbound", _texto("Já vendi")).status_code == 200
    assert ganchos[-1] == "regua_proprietario" and len(ganchos) == 5


def test_linha_da_assistente_mensagem_velha_so_grava(client, gravadas, monkeypatch):
    repasses: list[Any] = []
    monkeypatch.setattr(cloud_assistente, "repassar_para_bridge",
                        lambda m, nome: repasses.append(m.get("id")) or True)
    monkeypatch.setattr(cloud_assistente, "_tratar_botao_corretora",
                        lambda *a, **k: repasses.append("botao") or True)
    botao = {"from": LEAD, "id": "wamid.B", "type": "button", "timestamp": "1791225000",
             "button": {"text": "Ver briefing"}}
    velho = client.post("/cloud/assistente/inbound", headers={"X-Cloud-Velho": "1"},
                        json={"messages": [_texto("me ajuda?"), botao]})
    assert velho.status_code == 200
    assert velho.json()["gravadas"] == 2 and velho.json()["repassadas"] == 0
    assert repasses == []
    novo = client.post("/cloud/assistente/inbound", json={"messages": [_texto("me ajuda?", "wamid.T2")]})
    assert novo.json()["repassadas"] == 1


def test_sem_header_o_pedido_nao_e_velho():
    class _Req:  # os testes antigos da Assistente chamam o handler com um objeto assim
        async def json(self) -> dict:
            return {"messages": []}

    assert corretor.mensagem_velha(_Req()) is False
    assert asyncio.run(cloud_assistente.assistente_inbound(_Req()))["gravadas"] == 0
