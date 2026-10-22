"""A mensagem que o CLIENTE lê usa o nome PÚBLICO da corretora.

Caso real (18/09/2026): a Kauane recebeu, pela linha 2591, "Aqui é a Pessoa3,
da Acme Imóveis". Pra ela quem atende é a **Pessoa** — é o nome que a Pessoa12 usa
no atendimento e o verified_name do número na Meta ("Pessoa Pessoa9 Acme").
O sistema se apresentou com um nome que a cliente nunca mais ouviria.

`corretoras.nome` é identidade INTERNA (painel, ranking, Kommo);
`corretoras.nome_publico` é o que vai no texto pro lead. Estes testes travam a
separação nos dois pontos que escrevem pro cliente.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db.session import SessionLocal
from app.services import handoff_remarketing as hr
from app.services import nome_publico

CORRETORA_A = 3


@pytest.fixture()
def cadastro():
    """Põe a Pessoa12 com nome interno != público (o estado real) e devolve ao fim.

    UPDATE, não DELETE+INSERT: o seed da suíte cria as corretoras 1-3 e outras
    tabelas têm FK pra elas — apagar a linha 3 derruba a suíte inteira.
    """
    with SessionLocal() as s:
        antes = s.execute(
            text("SELECT nome_publico FROM corretoras WHERE id = :i"), {"i": CORRETORA_A}
        ).scalar_one()
        s.execute(
            text("UPDATE corretoras SET nome_publico = 'Pessoa' WHERE id = :i"),
            {"i": CORRETORA_A},
        )
        s.commit()
    try:
        yield
    finally:
        with SessionLocal() as s:
            s.execute(
                text("UPDATE corretoras SET nome_publico = :v WHERE id = :i"),
                {"v": antes, "i": CORRETORA_A},
            )
            s.commit()


# ── o helper ────────────────────────────────────────────────────────────────

def test_publico_devolve_pessoa_para_corretora_a(cadastro):
    with SessionLocal() as s:
        assert nome_publico.publico(s, CORRETORA_A) == "Pessoa"


def test_publico_cai_no_primeiro_nome_quando_nao_ha_publico():
    """Corretora sem nome_publico segue o comportamento histórico."""
    assert nome_publico.publico_de("Pessoa5 Souza", None) == "Pessoa5"


def test_publico_usa_so_o_primeiro_nome_do_publico():
    assert nome_publico.publico_de("Pessoa3", "Pessoa Pessoa9") == "Pessoa"


def test_publico_de_corretora_inexistente_e_none(cadastro):
    """None ≠ nome errado: quem chama decide o fallback ('a equipe')."""
    with SessionLocal() as s:
        assert nome_publico.publico(s, 99999) is None


# ── o texto que sai pro lead ────────────────────────────────────────────────

def test_texto_do_handoff_fala_pessoa_nao_corretora_a():
    # (texto do aviso desde 05/10/2026: o que a Dona ditou, "Sou a X, da Acme…")
    t = hr.texto_aviso("Pessoa")
    assert "Sou a Pessoa, da Acme Imóveis" in t
    assert "Pessoa3" not in t
    # a forma com resumo (opção desligada por padrão) também usa o nome público
    assert "Aqui é a Pessoa, da Acme Imóveis" in hr.texto_aviso("Pessoa", "é pra morar")


def test_texto_do_handoff_sem_nome_fala_pela_casa():
    assert hr.texto_aviso(None).startswith("Sou da equipe, da Acme")
    assert hr.texto_aviso(None, "é pra morar").startswith("Aqui é a equipe, da Acme")
