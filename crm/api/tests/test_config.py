"""Testes do guard ANTHROPIC_API_KEY + llm_provider=cli."""
import logging

import pytest

from app.config import _warn_if_cli_key_conflict


def test_warning_quando_api_key_setada_em_modo_cli(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.config"):
        _warn_if_cli_key_conflict("cli", "sk-ant-alguma-coisa")
    assert caplog.records, "esperava ao menos um registro de warning"
    assert "ANTHROPIC_API_KEY" in caplog.text


def test_sem_warning_quando_api_key_vazia_em_modo_cli(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.config"):
        _warn_if_cli_key_conflict("cli", "")
    assert not caplog.records


def test_sem_warning_quando_api_key_setada_em_modo_api(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.config"):
        _warn_if_cli_key_conflict("api", "sk-ant-alguma-coisa")
    assert not caplog.records
