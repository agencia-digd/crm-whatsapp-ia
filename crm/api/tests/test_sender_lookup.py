"""Testes do sender_lookup contra o DB de teste — Onda 2.

Telefones vêm das constantes de `tests/conftest.py` (fonte única do seed), não
de literais: o número da Pessoa3 já mudou uma vez (+5500000000000 →
+5500000000000) e o literal hardcoded aqui ficou apontando pro número velho.
Assertar contra o valor SEMEADO garante que o teste acompanha a próxima troca.
"""
from __future__ import annotations

import pytest

from app.services.sender_lookup import identify_sender

from tests.conftest import (
    TEL_CORRETORA_B,
    TEL_DANIELA_REMOVIDA,
    TEL_DESCONHECIDO,
    TEL_DONA,
    TEL_CORRETORA_A,
    TEL_CORRETORA_C,
)


def test_corretora_a_eh_corretora():
    s = identify_sender(TEL_CORRETORA_A)
    assert s.kind == "corretora"
    assert s.nome == "Pessoa3"
    assert s.papel == "corretora_atendedora_de_dona"


def test_corretora_b_eh_corretora():
    s = identify_sender(TEL_CORRETORA_B)
    assert s.kind == "corretora"
    assert s.nome == "Pessoa1"
    assert s.papel == "corretora_solo"


@pytest.mark.skip(reason="Pessoa5 offline (device removed 07/05); número certo no DB, lookup independe de conexão")
def test_corretora_c_eh_corretora():
    s = identify_sender(TEL_CORRETORA_C)
    assert s.kind == "corretora"
    assert s.nome == "Pessoa5"


def test_pessoa_eh_dona():
    s = identify_sender(TEL_DONA)
    assert s.kind == "dona"
    assert s.nome == "Pessoa Pessoa9 Acme"
    assert s.papel == "dona"


def test_daniela_removida_eh_unknown():
    """Onda P1 (2026-05-09): Dona saiu de internos_acme (modo_teste caducou)."""
    s = identify_sender(TEL_DANIELA_REMOVIDA)
    assert s.kind == "unknown"


def test_aleatorio_eh_unknown():
    s = identify_sender(TEL_DESCONHECIDO)
    assert s.kind == "unknown"
    assert s.id is None
