"""Canal Cloud da Assistente (app/api/cloud_assistente.py).

Tudo mockado: sem rede, sem banco. `gravar`/`aplicar_status` (DB) e `httpx` são
monkeypatchados. Cobre: adaptador Cloud→bridge, echo NÃO repassa pra bridge,
envio bloqueado sob pytest, e fora-de-janela (131047) estruturado.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.api import cloud_assistente as cd


# ─── adaptador Cloud → shape do webhook da bridge ────────────────────────────
def test_adaptar_para_bridge_shape() -> None:
    msg = {"from": "5500000000000", "type": "text", "text": {"body": "oi"}, "id": "wamid.A"}
    env = cd.adaptar_para_bridge(msg, "Fulano")
    change = env["entry"][0]["changes"][0]
    value = change["value"]
    assert change["field"] == "messages"
    assert value["messaging_product"] == "whatsapp"
    assert value["metadata"]["phone_number_id"] == cd.PHONE_ID
    assert value["messages"][0]["id"] == "wamid.A"
    assert value["contacts"][0]["wa_id"] == "5500000000000"
    assert value["contacts"][0]["profile"]["name"] == "Fulano"


def test_adaptar_sem_nome_usa_placeholder() -> None:
    env = cd.adaptar_para_bridge({"from": "5500000000000", "type": "text"}, None)
    assert env["entry"][0]["changes"][0]["value"]["contacts"][0]["profile"]["name"] == "?"


def test_adaptar_bridge_parseavel_pelo_cloudapi() -> None:
    """O envelope adaptado tem que ser digerível por cloudapi.parse_inbound da
    bridge. Reproduzimos o parser aqui (mesma lógica documentada) pra travar o
    contrato sem importar /opt no CI."""
    msg = {"from": "5500000000000", "type": "text", "text": {"body": "quero visitar"},
           "id": "wamid.B"}
    env = cd.adaptar_para_bridge(msg, "Cliente")
    # parser equivalente ao cloudapi.parse_inbound
    got = []
    for entry in env.get("entry", []):
        for ch in entry.get("changes", []):
            val = ch.get("value") or {}
            contatos = {c.get("wa_id"): (c.get("profile") or {}).get("name")
                        for c in (val.get("contacts") or [])}
            for m in (val.get("messages") or []):
                txt = (m.get("text") or {}).get("body", "")
                got.append({"from": m.get("from"), "text": txt,
                            "name": contatos.get(m.get("from")), "id": m.get("id")})
    assert got == [{"from": "5500000000000", "text": "quero visitar",
                    "name": "Cliente", "id": "wamid.B"}]


# ─── echo NÃO repassa pra bridge ─────────────────────────────────────────────
def test_echo_grava_mas_nao_repassa(monkeypatch) -> None:
    gravados: list[dict[str, Any]] = []
    forwards: list[Any] = []

    def fake_gravar(**kw: Any) -> bool:
        gravados.append(kw)
        return True

    def fake_forward(*a: Any, **k: Any) -> bool:
        forwards.append((a, k))
        return True

    monkeypatch.setattr(cd, "gravar", fake_gravar)
    monkeypatch.setattr(cd, "repassar_para_bridge", fake_forward)

    class _Req:
        async def json(self) -> dict:
            return {"echoes": [{
                "from": "+5500000000000", "to": "5500000000000", "id": "wamid.E",
                "type": "text", "text": {"body": "te retorno já"},
            }]}

    res = asyncio.run(cd.assistente_echo(_Req()))
    assert res["gravados"] == 1
    assert gravados and gravados[0]["from_corretora"] is True
    # eco NUNCA vai pra bridge
    assert forwards == []


# ─── inbound grava E repassa pra bridge ──────────────────────────────────────
def test_inbound_grava_e_repassa(monkeypatch) -> None:
    forwards: list[Any] = []

    monkeypatch.setattr(cd, "gravar", lambda **kw: True)
    monkeypatch.setattr(
        cd, "repassar_para_bridge",
        lambda m, nome: forwards.append((m.get("id"), nome)) or True,
    )

    class _Req:
        async def json(self) -> dict:
            return {
                "messages": [{"from": "5500000000000", "type": "text",
                              "text": {"body": "oi"}, "id": "wamid.I"}],
                "contacts": [{"wa_id": "5500000000000", "profile": {"name": "Zé"}}],
            }

    res = asyncio.run(cd.assistente_inbound(_Req()))
    assert res["gravadas"] == 1
    assert res["repassadas"] == 1
    assert forwards == [("wamid.I", "Zé")]


def test_inbound_duplicado_nao_repassa(monkeypatch) -> None:
    """gravar() devolve False em dup (idempotência wamid) → NÃO repassa."""
    forwards: list[Any] = []
    monkeypatch.setattr(cd, "gravar", lambda **kw: False)
    monkeypatch.setattr(cd, "repassar_para_bridge",
                        lambda m, nome: forwards.append(m) or True)

    class _Req:
        async def json(self) -> dict:
            return {"messages": [{"from": "5500000000000", "type": "text",
                                  "text": {"body": "oi"}, "id": "wamid.DUP"}]}

    res = asyncio.run(cd.assistente_inbound(_Req()))
    assert res["gravadas"] == 0
    assert res["repassadas"] == 0
    assert forwards == []


# ─── envio bloqueado sob pytest (guarda anti-teste) ──────────────────────────
def test_envio_bloqueado_sob_pytest(monkeypatch) -> None:
    # httpx.Client não deve NEM ser instanciado sob pytest
    def _boom(*a: Any, **k: Any):
        raise AssertionError("NUNCA deve chamar httpx sob pytest")

    monkeypatch.setattr(cd.httpx, "Client", _boom)
    # PYTEST_CURRENT_TEST está setado (pytest roda) → guarda ativa
    res = cd.enviar_cloud_assistente("5500000000000", "resposta")
    assert res["sent"] is False
    assert res.get("pytest") is True


# ─── fora-de-janela (131047) estruturado, sem retry/template ─────────────────
class _FakeResp:
    def __init__(self, status: int, body: dict) -> None:
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self) -> dict:
        return self._body


class _FakeClient:
    """Context manager que devolve uma resposta fixa em .post()."""

    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def post(self, *a: Any, **k: Any) -> _FakeResp:
        return self._resp


def _desarma_guarda_pytest(monkeypatch) -> None:
    """Remove PYTEST_CURRENT_TEST só dentro do teste, pra exercitar o caminho de
    envio real — mas httpx segue mockado, então nenhuma rede acontece."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)


def test_fora_de_janela_estruturado(monkeypatch) -> None:
    _desarma_guarda_pytest(monkeypatch)
    monkeypatch.setattr(cd.cloud_lines, "dry_run_for", lambda line: False)
    monkeypatch.setattr(cd.cloud_lines, "token_for", lambda line: "tok-fake")
    gravou: list[Any] = []
    monkeypatch.setattr(cd, "gravar", lambda **kw: gravou.append(kw) or True)

    resp = _FakeResp(400, {"error": {"code": cd.WINDOW_CLOSED_CODE,
                                     "message": "re-engagement message"}})
    monkeypatch.setattr(cd.httpx, "Client", lambda *a, **k: _FakeClient(resp))

    res = cd.enviar_cloud_assistente("5500000000000", "oi tarde demais")
    assert res["ok"] is False
    assert res["error"] == "fora_de_janela"
    assert res["code"] == cd.WINDOW_CLOSED_CODE
    assert res["sent"] is False
    # NÃO grava outbound (não saiu), NÃO retenta, NÃO manda template
    assert gravou == []


def test_envio_ok_grava_outbound(monkeypatch) -> None:
    _desarma_guarda_pytest(monkeypatch)
    monkeypatch.setattr(cd.cloud_lines, "dry_run_for", lambda line: False)
    monkeypatch.setattr(cd.cloud_lines, "token_for", lambda line: "tok-fake")
    gravou: list[Any] = []
    monkeypatch.setattr(cd, "gravar", lambda **kw: gravou.append(kw) or True)

    resp = _FakeResp(200, {"messages": [{"id": "wamid.OUT"}]})
    monkeypatch.setattr(cd.httpx, "Client", lambda *a, **k: _FakeClient(resp))

    res = cd.enviar_cloud_assistente("5500000000000", "chego já")
    assert res["ok"] is True
    assert res["sent"] is True
    assert res["wamid"] == "wamid.OUT"
    assert gravou and gravou[0]["from_corretora"] is True


def test_envio_dry_run_nao_chama_httpx(monkeypatch) -> None:
    _desarma_guarda_pytest(monkeypatch)
    monkeypatch.setattr(cd.cloud_lines, "dry_run_for", lambda line: True)
    monkeypatch.setattr(cd, "gravar", lambda **kw: True)
    monkeypatch.setattr(cd.httpx, "Client",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("dry_run não deve chamar httpx")))
    res = cd.enviar_cloud_assistente("5500000000000", "seria enviado")
    assert res["dry_run"] is True
    assert res["sent"] is False


# ─── endpoint /enviar: auth por secret (fail-closed) ─────────────────────────
def test_enviar_endpoint_sem_secret_configurado_fecha(monkeypatch) -> None:
    monkeypatch.setattr(cd.settings, "assistente_enviar_secret", "")

    class _Req:
        async def json(self) -> dict:
            return {"to": "5500000000000", "texto": "oi"}

    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(cd.assistente_enviar(_Req(), x_bridge_secret="qualquer"))
    assert exc.value.status_code == 401


def test_enviar_endpoint_secret_errado_fecha(monkeypatch) -> None:
    monkeypatch.setattr(cd.settings, "assistente_enviar_secret", "certo")

    class _Req:
        async def json(self) -> dict:
            return {"to": "5500000000000", "texto": "oi"}

    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(cd.assistente_enviar(_Req(), x_bridge_secret="errado"))
    assert exc.value.status_code == 401


def test_enviar_endpoint_secret_ok_chama_envio(monkeypatch) -> None:
    monkeypatch.setattr(cd.settings, "assistente_enviar_secret", "certo")
    chamou: list[Any] = []
    monkeypatch.setattr(cd, "enviar_cloud_assistente",
                        lambda to, texto: chamou.append((to, texto)) or {"ok": True, "sent": False})

    class _Req:
        async def json(self) -> dict:
            return {"to": "5500000000000", "texto": "resposta"}

    res = asyncio.run(cd.assistente_enviar(_Req(), x_bridge_secret="certo"))
    assert res["ok"] is True
    assert chamou == [("5500000000000", "resposta")]


def test_enviar_endpoint_falta_campo(monkeypatch) -> None:
    monkeypatch.setattr(cd.settings, "assistente_enviar_secret", "certo")

    class _Req:
        async def json(self) -> dict:
            return {"to": "5500000000000"}  # sem texto

    res = asyncio.run(cd.assistente_enviar(_Req(), x_bridge_secret="certo"))
    assert res["ok"] is False
    assert res["error"] == "to_e_texto_obrigatorios"
