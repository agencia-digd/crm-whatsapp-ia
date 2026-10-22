"""Testes do plug do GlitchTip — o principal é: nunca derrubar o app."""
from __future__ import annotations

import pytest

from app import observability
from app.config import settings


def test_sem_dsn_nao_liga_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    chamou = False

    def _fake_init(**_kwargs: object) -> None:
        nonlocal chamou
        chamou = True

    monkeypatch.setattr(settings, "glitchtip_dsn", "")
    monkeypatch.setattr("sentry_sdk.init", _fake_init)

    assert observability.init_observability() is False
    assert not chamou


def test_com_dsn_inicializa_com_environment_e_release(monkeypatch: pytest.MonkeyPatch) -> None:
    capturado: dict[str, object] = {}

    def _fake_init(**kwargs: object) -> None:
        capturado.update(kwargs)

    monkeypatch.setattr(settings, "glitchtip_dsn", "http://contato@example.com:8123/1")
    monkeypatch.setattr(settings, "env", "staging")
    monkeypatch.setattr("sentry_sdk.init", _fake_init)

    assert observability.init_observability() is True
    assert capturado["dsn"] == "http://contato@example.com:8123/1"
    assert capturado["environment"] == "staging"
    assert capturado["send_default_pii"] is False
    release = capturado["release"]
    assert release is None or str(release).startswith("copiloto-acme@")


def test_falha_no_init_nao_propaga(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(**_kwargs: object) -> None:
        raise RuntimeError("DSN inválido")

    monkeypatch.setattr(settings, "glitchtip_dsn", "http://contato@example.com:8123/1")
    monkeypatch.setattr("sentry_sdk.init", _boom)

    assert observability.init_observability() is False


def test_release_do_git_ou_none() -> None:
    release = observability._derive_release()
    assert release is None or release.startswith("copiloto-acme@")
