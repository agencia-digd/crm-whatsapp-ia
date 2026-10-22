"""Mídia do painel sai pela linha Cloud DA DONA do lead (24/09 — foto da Pessoa7).

Antes só a Pessoa12 ia por Cloud; Pessoa7/Corretor/Pessoa10/Pessoa11 caíam no Evolution e davam 404
em /message/sendMedia/<instance>.
"""
from __future__ import annotations

from typing import Any

from app.api import cloud_corretora_a as cg
from app.services.cloud_lines import CloudLine


class _Resp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.status_code = 200
        self._p = payload
        self.text = ""

    def json(self) -> dict[str, Any]:
        return self._p


def _fake_client(chamadas: list[tuple[str, str | None]]):
    class _C:
        def __init__(self, *a: Any, **k: Any) -> None: ...
        def __enter__(self): return self
        def __exit__(self, *a: Any) -> None: ...
        def post(self, url: str, headers: dict[str, str], **k: Any) -> _Resp:
            chamadas.append((url, headers.get("Authorization")))
            if url.endswith("/media"):
                return _Resp({"id": "media-1"})
            return _Resp({"messages": [{"id": "wamid-1"}]})
    return _C


def test_midia_com_linha_usa_phone_id_e_token_da_linha(monkeypatch) -> None:
    chamadas: list[tuple[str, str | None]] = []
    gravados: list[dict[str, Any]] = []
    monkeypatch.setattr(cg, "_dry_run_ativo", lambda: False)
    monkeypatch.setattr(cg.httpx, "Client", _fake_client(chamadas))
    monkeypatch.setattr(cg, "gravar", lambda **k: gravados.append(k) or True)
    monkeypatch.setattr(cg.cloud_lines, "token_for", lambda l: "tok-pessoa_d")
    linha = CloudLine(phone_id="PID-PESSOA_D", slug="pessoa_d", papel="corretora",
                      env_file="/dev/null", token_var="X")

    cg.enviar_cloud_midia("+5500000000000", b"img", mimetype="image/jpeg",
                          filename="f.jpg", tipo="image", caption="oi",
                          linha=linha, instance="acme_pessoa_d", corretora_id=7)

    assert [u for u, _ in chamadas] == [
        f"https://graph.facebook.com/{cg.API_VER}/PID-PESSOA_D/media",
        f"https://graph.facebook.com/{cg.API_VER}/PID-PESSOA_D/messages",
    ]
    assert all(a == "Bearer tok-pessoa_d" for _, a in chamadas)
    assert gravados[0]["instance"] == "acme_pessoa_d"
    assert gravados[0]["corretora_id"] == 7


def test_midia_sem_linha_segue_no_2591_da_corretor(monkeypatch) -> None:
    chamadas: list[tuple[str, str | None]] = []
    monkeypatch.setattr(cg, "_dry_run_ativo", lambda: False)
    monkeypatch.setattr(cg.httpx, "Client", _fake_client(chamadas))
    monkeypatch.setattr(cg, "gravar", lambda **k: True)
    monkeypatch.setattr(cg, "_token", lambda: "tok-corretor")

    cg.enviar_cloud_midia("+5500000000000", b"img", mimetype="image/jpeg",
                          filename="f.jpg", tipo="image")

    assert chamadas[0][0].endswith(f"/{cg.PHONE_ID}/media")
    assert chamadas[0][1] == "Bearer tok-corretor"
