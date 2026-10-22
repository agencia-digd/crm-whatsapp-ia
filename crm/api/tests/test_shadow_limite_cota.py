"""Limite de cota do `claude -p` (sessão/semana): erro permanente, sem retry, rodada para."""
import pytest

from app.inspector import shadow
from app.llm.cli_provider import (
    _classify_stderr,
    e_limite_de_cota,
)

MSGS = [
    "You've hit your session limit · resets 7pm (UTC)",
    "You've hit your weekly limit · resets Oct 24",
    "Claude AI usage limit reached|1760000000",
]


@pytest.mark.parametrize("msg", MSGS)
def test_limite_de_cota_e_permanente(msg):
    assert e_limite_de_cota(msg)
    assert _classify_stderr("", msg) is False  # não é transitório: sem retry


def test_erro_comum_continua_transitorio():
    assert not e_limite_de_cota("connection reset")
    assert _classify_stderr("connection reset", "") is True


class _ProvLimite:
    def complete(self, system, user, schema):
        raise RuntimeError("ClaudeCliProvider: exit 1 (permanente): stdout: " + MSGS[0])


class _ProvQuebrado:
    def complete(self, system, user, schema):
        raise RuntimeError("boom")


CONVERSA = [{"from": "lead", "text": "oi, quero visitar", "at": "2026-10-20T10:00:00"}]


def _chamar(prov):
    return shadow.decidir_sombra("Pessoa3", 3, "Fulano", "1", "+5500000000000", CONVERSA, provider=prov)


def test_decidir_sombra_sobe_limite_de_cota():
    with pytest.raises(shadow.ShadowLimiteCota):
        _chamar(_ProvLimite())


def test_outro_erro_continua_none():
    assert _chamar(_ProvQuebrado()) is None
