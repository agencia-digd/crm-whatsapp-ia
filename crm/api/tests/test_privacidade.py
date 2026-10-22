"""Regra de ouro de privacidade — e2e probabilístico."""
from __future__ import annotations

import pytest


@pytest.mark.skip(reason="e2e probabilístico (depende do LLM real); reabilitar em suite e2e dedicada")
def test_corretora_nao_recebe_dado_de_outra():
    pass


@pytest.mark.skip(reason="e2e probabilístico; reabilitar em suite e2e dedicada")
def test_dona_pode_ver_dado_de_qualquer_corretora():
    pass
