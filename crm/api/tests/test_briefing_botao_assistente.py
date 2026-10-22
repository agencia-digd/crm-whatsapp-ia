"""Fluxo do botão do template de briefing na linha da Assistente (cloud_assistente).

Cobre (tudo mockado — sem banco, sem rede; PYTEST_CURRENT_TEST + CLOUD_DRY_RUN
são as redes de segurança contra envio real):
  - clique "Saiba mais" de CORRETORA → entrega o briefing completo, NÃO repassa
    pra bridge;
  - clique "Parar mensagens" de CORRETORA → registra opt-out + confirma, NÃO
    repassa pra bridge;
  - botão de número que NÃO é corretora → segue pro fluxo normal (bridge);
  - texto normal de corretora → segue pro fluxo normal (bridge);
  - duplicata (dedup por wamid) → não trata botão de novo;
  - endpoint /cloud/assistente/briefing/aviso: fail-closed sem X-Internal-Token,
    pula opt-out, despacha o template pra corretora elegível;
  - guarda pytest: enviar_aviso_briefing NUNCA envia sob pytest.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.api.cloud_assistente as cd
from app.main import app
from app.services import briefing_push


class FakeCorretora:
    id = 3
    nome = "Pessoa3 Silva"
    telefone_normalizado = "+5500000000000"
    ativo = True
    briefing_optout = False


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def base_mocks(monkeypatch):
    """Mocks comuns: gravar OK, bridge espiada, corretora resolvida."""
    forwards: list[Any] = []
    monkeypatch.setattr(cd, "gravar", lambda **kw: True)
    monkeypatch.setattr(
        cd, "repassar_para_bridge",
        lambda m, nome: forwards.append((m.get("id"), nome)) or True,
    )
    monkeypatch.setattr(cd, "_corretora_por_telefone", lambda tel: FakeCorretora())
    return forwards


def _payload_botao(texto_botao: str, frm: str = "+5500000000000") -> dict:
    return {
        "messages": [{
            "from": frm, "type": "button", "id": "wamid.BTN1",
            "button": {"text": texto_botao, "payload": texto_botao},
            "context": {"id": "wamid.TEMPLATE"},
        }],
        "contacts": [{"wa_id": frm, "profile": {"name": "Pessoa3"}}],
    }


# ─── clique "Saiba mais" → briefing completo, sem bridge ────────────────────
def test_botao_saiba_mais_entrega_briefing(client, base_mocks, monkeypatch):
    chamadas: list[tuple[int, str]] = []
    monkeypatch.setattr(
        briefing_push, "entregar_briefing_completo",
        lambda cid, tel: chamadas.append((cid, tel)) or {"ok": True, "sent": False},
    )
    resp = client.post("/cloud/assistente/inbound", json=_payload_botao("Saiba mais"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["botoes_tratados"] == 1
    assert body["repassadas"] == 0
    # BackgroundTasks do TestClient rodam na resposta
    assert chamadas == [(3, "+5500000000000")]
    assert base_mocks == []  # bridge NÃO viu o clique


# ─── clique "Parar mensagens" → opt-out + confirmação, sem bridge ───────────
def test_botao_parar_mensagens_optout(client, base_mocks, monkeypatch):
    optouts: list[int] = []
    confirmacoes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        briefing_push, "registrar_optout", lambda cid: optouts.append(cid) or True,
    )
    monkeypatch.setattr(
        cd, "enviar_cloud_assistente",
        lambda to, texto: confirmacoes.append((to, texto)) or {"ok": True, "sent": False},
    )
    resp = client.post("/cloud/assistente/inbound", json=_payload_botao("Parar mensagens"))
    assert resp.status_code == 200
    assert resp.json()["botoes_tratados"] == 1
    assert optouts == [3]
    assert confirmacoes and confirmacoes[0][0] == "+5500000000000"
    assert "não vai mais receber" in confirmacoes[0][1]
    assert base_mocks == []


# ─── botão de quem NÃO é corretora → bridge decide (fluxo atual) ────────────
def test_botao_nao_corretora_vai_pra_bridge(client, base_mocks, monkeypatch):
    monkeypatch.setattr(cd, "_corretora_por_telefone", lambda tel: None)
    resp = client.post("/cloud/assistente/inbound",
                       json=_payload_botao("Saiba mais", frm="+5500000000000"))
    body = resp.json()
    assert body["botoes_tratados"] == 0
    assert body["repassadas"] == 1
    assert base_mocks == [("wamid.BTN1", "Pessoa3")]


# ─── texto normal de corretora → bridge (Assistente responde, como hoje) ───────
def test_texto_de_corretora_segue_pra_bridge(client, base_mocks):
    payload = {
        "messages": [{"from": "+5500000000000", "type": "text",
                      "text": {"body": "me manda o briefing"}, "id": "wamid.T1"}],
        "contacts": [{"wa_id": "+5500000000000", "profile": {"name": "Pessoa3"}}],
    }
    resp = client.post("/cloud/assistente/inbound", json=payload)
    body = resp.json()
    assert body["botoes_tratados"] == 0
    assert body["repassadas"] == 1


# ─── duplicata (dedup wamid) → não trata botão nem repassa ──────────────────
def test_botao_duplicado_nao_trata(client, monkeypatch):
    monkeypatch.setattr(cd, "gravar", lambda **kw: False)  # dup
    tratados: list[Any] = []
    monkeypatch.setattr(
        briefing_push, "entregar_briefing_completo",
        lambda cid, tel: tratados.append(cid) or {"ok": True},
    )
    monkeypatch.setattr(cd, "repassar_para_bridge", lambda m, nome: True)
    monkeypatch.setattr(cd, "_corretora_por_telefone", lambda tel: FakeCorretora())
    resp = client.post("/cloud/assistente/inbound", json=_payload_botao("Saiba mais"))
    body = resp.json()
    assert body["gravadas"] == 0
    assert body["botoes_tratados"] == 0
    assert tratados == []


# ─── endpoint /briefing/aviso ────────────────────────────────────────────────
class _FakeResult:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    def scalar_one_or_none(self) -> Any:
        return self._obj


class _FakeSession:
    def __init__(self, corretora: Any) -> None:
        self._corretora = corretora

    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def execute(self, *a: Any, **k: Any) -> _FakeResult:
        return _FakeResult(self._corretora)


def test_aviso_sem_token_fecha(client, monkeypatch):
    monkeypatch.setattr(cd.settings, "copiloto_internal_token", "tok-interno")
    resp = client.post("/cloud/assistente/briefing/aviso",
                       json={"corretora_id": 3, "edicao": "seg"})
    assert resp.status_code == 401


def test_aviso_token_vazio_fecha(client, monkeypatch):
    # fail-closed: sem token configurado, NINGUÉM dispara
    monkeypatch.setattr(cd.settings, "copiloto_internal_token", "")
    resp = client.post("/cloud/assistente/briefing/aviso",
                       json={"corretora_id": 3, "edicao": "seg"},
                       headers={"X-Internal-Token": "qualquer"})
    assert resp.status_code == 401


def test_aviso_pula_optout(client, monkeypatch):
    monkeypatch.setattr(cd.settings, "copiloto_internal_token", "tok-interno")
    corr = FakeCorretora()
    corr.briefing_optout = True
    monkeypatch.setattr(cd, "SessionLocal", lambda: _FakeSession(corr))
    enviados: list[Any] = []
    monkeypatch.setattr(
        briefing_push, "enviar_aviso_briefing",
        lambda **kw: enviados.append(kw) or {"ok": True, "sent": True},
    )
    resp = client.post("/cloud/assistente/briefing/aviso",
                       json={"corretora_id": 3, "edicao": "seg"},
                       headers={"X-Internal-Token": "tok-interno"})
    assert resp.status_code == 200
    assert resp.json()["skipped"] == "optout"
    assert enviados == []


def test_aviso_despacha_template(client, monkeypatch):
    monkeypatch.setattr(cd.settings, "copiloto_internal_token", "tok-interno")
    monkeypatch.setattr(cd, "SessionLocal", lambda: _FakeSession(FakeCorretora()))
    enviados: list[dict] = []
    monkeypatch.setattr(
        briefing_push, "enviar_aviso_briefing",
        lambda **kw: enviados.append(kw) or {"ok": True, "sent": True, "wamid": "w1"},
    )
    resp = client.post("/cloud/assistente/briefing/aviso",
                       json={"corretora_id": 3, "edicao": "sex"},
                       headers={"X-Internal-Token": "tok-interno"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert enviados == [{
        "corretora_id": 3, "nome": "Pessoa3 Silva",
        "telefone": "+5500000000000", "edicao": "sex",
    }]


def test_aviso_edicao_invalida_422(client, monkeypatch):
    monkeypatch.setattr(cd.settings, "copiloto_internal_token", "tok-interno")
    resp = client.post("/cloud/assistente/briefing/aviso",
                       json={"corretora_id": 3, "edicao": "qua"},
                       headers={"X-Internal-Token": "tok-interno"})
    assert resp.status_code == 422


# ─── guarda pytest: NUNCA envia template de verdade sob teste ────────────────
def test_enviar_aviso_bloqueado_sob_pytest(monkeypatch):
    def _boom(*a: Any, **k: Any):
        raise AssertionError("NUNCA deve chamar httpx sob pytest")

    monkeypatch.setattr(briefing_push.httpx, "Client", _boom)
    res = briefing_push.enviar_aviso_briefing(
        corretora_id=3, nome="Pessoa3", telefone="+5500000000000", edicao="seg",
    )
    assert res["sent"] is False
    assert res.get("pytest") is True


def test_pool_da_edicao_invalida():
    with pytest.raises(ValueError):
        briefing_push.pool_da_edicao("qua")
