"""Testes do módulo de persona `metodo_pessoa_e` — Onda 1 do refactor do squad Acme.

Garante que a régua diagnóstica da Pessoa foi extraída FIEL do texto-fonte
(`app.services.atendimento_brain.METODO`) e que as 9 âncoras do diagnóstico estão presentes.
"""

from __future__ import annotations

from app.persona.metodo_pessoa_e import METODO_PESSOA_E, aplicar_metodo
from app.services.atendimento_brain import METODO as METODO_FONTE

# As 9 âncoras do diagnóstico (uma por pergunta) + as duas marcas da filosofia.
ANCORAS_DIAGNOSTICO = [
    "REALMENTE busca",          # 1
    "Quanto pretende investir",  # 2
    "comprando AGORA",           # 3
    "se NÃO resolver",           # 4
    "solução ideal PRA ELE",     # 5
    "pesa mais na decisão",      # 6
    "Quem mais decide",          # 7
    "Há quanto tempo procura",   # 8
    "pra FECHAR",                # 9
]


def test_metodo_pessoa_e_contem_as_9_ancoras() -> None:
    for ancora in ANCORAS_DIAGNOSTICO:
        assert ancora in METODO_PESSOA_E, f"Âncora ausente na régua: {ancora!r}"


def test_filosofia_pare_de_convencer_comece_a_entender() -> None:
    assert "Pare de tentar CONVENCER" in METODO_PESSOA_E
    assert "Comece a ENTENDER" in METODO_PESSOA_E


def test_metodo_pessoa_e_identico_a_fonte() -> None:
    # A extração tem que ser 100% fiel ao conhecimento validado em atendimento_brain.py.
    assert METODO_PESSOA_E == METODO_FONTE


def test_aplicar_metodo_retorna_a_regua() -> None:
    assert aplicar_metodo() == METODO_PESSOA_E
