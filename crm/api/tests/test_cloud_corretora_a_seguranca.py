"""Furos de segurança fechados em app/api/cloud_corretora_a.py (review 2026-07-10).

Cobre:
  (1) gate de dry_run nas 3 funções de envio (enviar_cloud, enviar_template_corretor,
      enviar_cloud_midia) — NUNCA chama a Graph API sob dry_run/pytest.
  (2) auth dos webhooks /cloud/{echo,status,inbound} — sem/errado X-Cloud-Secret
      → HTTP 401 (fail-closed).

Tudo mockado: sem rede, sem banco. `gravar` (DB) e `httpx` são monkeypatchados.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import HTTPException

from app.api import cloud_corretora_a as corretor


def _boom_httpx(*a: Any, **k: Any):
    raise AssertionError("dry_run/pytest NUNCA deve instanciar httpx.Client")


# ─── (a) dry_run não chama a Graph ───────────────────────────────────────────
def test_enviar_cloud_dry_run_nao_chama_graph(monkeypatch) -> None:
    # sob pytest PYTEST_CURRENT_TEST já está setado → _dry_run_ativo() True.
    monkeypatch.setattr(corretor.httpx, "Client", _boom_httpx)
    gravou: list[dict[str, Any]] = []
    monkeypatch.setattr(corretor, "gravar", lambda **kw: gravou.append(kw) or True)

    res = corretor.enviar_cloud("+5500000000000", "seria enviado")

    assert res["dry_run"] is True
    assert res["sent"] is False
    assert gravou and gravou[0]["from_corretora"] is True


def test_enviar_template_corretor_dry_run_nao_chama_graph(monkeypatch) -> None:
    monkeypatch.setattr(corretor.httpx, "Client", _boom_httpx)
    # se chegasse a resolver token, quebraria em worktree — dry_run deve cortar antes.
    monkeypatch.setattr(corretor, "_token", _boom_httpx)
    gravou: list[dict[str, Any]] = []
    monkeypatch.setattr(corretor, "gravar", lambda **kw: gravou.append(kw) or True)

    res = corretor.enviar_template_corretor("+5500000000000", "João", hook="oi de novo")

    assert res["dry_run"] is True
    assert res["sent"] is False
    assert gravou and gravou[0]["tipo"] == "template"


def test_enviar_cloud_midia_dry_run_nao_chama_graph(monkeypatch) -> None:
    monkeypatch.setattr(corretor.httpx, "Client", _boom_httpx)
    monkeypatch.setattr(corretor, "_token", _boom_httpx)
    gravou: list[dict[str, Any]] = []
    monkeypatch.setattr(corretor, "gravar", lambda **kw: gravou.append(kw) or True)

    res = corretor.enviar_cloud_midia(
        "+5500000000000", b"\x00\x01", mimetype="image/jpeg",
        filename="foto.jpg", tipo="image", caption="olha",
    )

    assert res["dry_run"] is True
    assert res["sent"] is False
    assert gravou and gravou[0]["tipo"] == "image"


def test_dry_run_via_settings_mesmo_sem_pytest(monkeypatch) -> None:
    """Fora do pytest (guard removido), settings.dry_run=True ainda corta o envio."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(corretor.settings, "dry_run", True)
    monkeypatch.setattr(corretor.httpx, "Client", _boom_httpx)
    monkeypatch.setattr(corretor, "gravar", lambda **kw: True)

    res = corretor.enviar_cloud("+5500000000000", "x")
    assert res["dry_run"] is True


# ─── (b) webhook sem/errado secret → 401 ─────────────────────────────────────
class _Req:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    async def json(self) -> dict[str, Any]:
        return self._body


@pytest.mark.parametrize("endpoint", ["cloud_echo", "cloud_status", "cloud_inbound"])
def test_webhook_sem_secret_configurado_fecha(monkeypatch, endpoint: str) -> None:
    """Secret vazio em settings → fail-closed (401), mesmo com header presente."""
    monkeypatch.setattr(corretor.settings, "cloud_webhook_secret", "")
    fn = getattr(corretor, endpoint)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(fn(_Req({}), x_cloud_secret="qualquer"))
    assert exc.value.status_code == 401


@pytest.mark.parametrize("endpoint", ["cloud_echo", "cloud_status", "cloud_inbound"])
def test_webhook_sem_header_fecha(monkeypatch, endpoint: str) -> None:
    monkeypatch.setattr(corretor.settings, "cloud_webhook_secret", "s3cr3t")
    fn = getattr(corretor, endpoint)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(fn(_Req({}), x_cloud_secret=None))
    assert exc.value.status_code == 401


@pytest.mark.parametrize("endpoint", ["cloud_echo", "cloud_status", "cloud_inbound"])
def test_webhook_secret_errado_fecha(monkeypatch, endpoint: str) -> None:
    monkeypatch.setattr(corretor.settings, "cloud_webhook_secret", "s3cr3t")
    fn = getattr(corretor, endpoint)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(fn(_Req({}), x_cloud_secret="errado"))
    assert exc.value.status_code == 401


def test_webhook_secret_ok_processa(monkeypatch) -> None:
    """Secret correto → passa da auth e processa payload vazio (0 gravados)."""
    monkeypatch.setattr(corretor.settings, "cloud_webhook_secret", "s3cr3t")
    res = asyncio.run(corretor.cloud_status(_Req({"statuses": []}), x_cloud_secret="s3cr3t"))
    assert res["ok"] is True
    assert res["aplicados"] == 0
