"""A trava de opt-out no ponto de envio — o furo de 16/09/2026.

Uma varredura achou 33 pessoas que pediram pra parar de receber mensagem e
SETE que receberam mensagem depois mesmo assim; em quatro delas quem mandou
foi o robô, com `[reengajamento:]`.

A detecção (`is_opt_out`) estava certa o tempo todo. O que faltava era:

  1. o pedido nem sempre era GRAVADO nas tabelas que as filas consultam;
  2. cada motor tinha (ou não tinha) a sua própria trava — `nutricao`,
     `pessoa_retomada` e `lead_intake` não consultavam opt-out de forma nenhuma;
  3. NENHUM motor conhecia a etapa "Não perturbe", então mover o card pra lá
     era proteção zero.

A correção põe a trava em `enviar_template_linha`, por onde todo template
automático passa, e cria uma fonte de verdade única em `opt_out.clausula()`.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text as sql_text

from app.services import opt_out


def test_detecta_o_botao_e_ignora_a_preposicao():
    """A regra canônica não mudou — este teste existe pra ela não regredir."""
    assert opt_out.is_opt_out("Parar mensagens") is True
    assert opt_out.is_opt_out("nao quero receber mais mensagens") is True
    assert opt_out.is_opt_out("me exclua da lista") is True
    # os falsos positivos que já custaram lead morto no funil
    assert opt_out.is_opt_out("quero um apê para minha mãe") is False
    assert opt_out.is_opt_out("preciso cancelar a visita de amanhã") is False
    assert opt_out.is_opt_out("Estamos ansiosos para atendê-lo") is False


def test_clausula_cobre_as_tres_fontes():
    """Disparo legado, cadência e a etapa Não perturbe — nesta ordem."""
    sql = opt_out.clausula("l.telefone")
    assert "disparos_reengajamento" in sql
    assert "cadencia_estado" in sql
    assert f"stage_id = {opt_out.ETAPA_NAO_PERTURBE}" in sql
    # a chave é sempre o sufixo-8: o mesmo humano aparece com +55, sem 55,
    # com e sem o nono dígito
    assert sql.count("'\\D', '', 'g'), 8)") >= 6


def test_clausula_aceita_qualquer_coluna():
    assert "lx.telefone" not in opt_out.clausula("m.contato_telefone").split("FROM leads")[0]
    assert "m.contato_telefone" in opt_out.clausula("m.contato_telefone")


@pytest.fixture()
def sessao():
    """Sessão no Postgres de teste, desfeita no fim — não suja o banco."""
    from app.db.session import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


def test_registrar_e_bloquear(sessao):
    """Ponta a ponta: registrar faz `esta_bloqueado` virar True."""
    tel = "+5500000000000"
    assert opt_out.esta_bloqueado(sessao, tel) is False

    assert opt_out.registrar(sessao, tel, motivo="teste") is True
    sessao.flush()

    assert opt_out.esta_bloqueado(sessao, tel) is True
    # idempotente: chamar de novo não quebra nem duplica
    assert opt_out.registrar(sessao, tel, motivo="teste") is True
    n = sessao.execute(
        sql_text("""SELECT count(*) FROM cadencia_estado
                     WHERE right(regexp_replace(telefone,'\\D','','g'),8) = :s"""),
        {"s": "99990001"},
    ).scalar()
    assert n == 1


def test_bloqueio_vale_com_telefone_em_outro_formato(sessao):
    """+5500000000000 e +5500000000000 são a mesma pessoa."""
    opt_out.registrar(sessao, "+5500000000000", motivo="teste")
    sessao.flush()
    for variante in ("+5500000000000", "+5500000000000", "+5500000000000"):
        assert opt_out.esta_bloqueado(sessao, variante) is True, variante


def test_telefone_curto_nao_registra(sessao):
    """Sufixo-8 incompleto marcaria gente demais — recusa e loga."""
    assert opt_out.registrar(sessao, "123", motivo="teste") is False
    assert opt_out.registrar(sessao, None, motivo="teste") is False


def test_sem_telefone_nao_bloqueia(sessao):
    assert opt_out.esta_bloqueado(sessao, None) is False
    assert opt_out.esta_bloqueado(sessao, "") is False


def test_envio_automatico_barra_e_humano_passa(monkeypatch, sessao):
    """O contrato que importa: `origem` preenchida = automação = barrada.

    Envio humano do painel chega com `origem=None` e não pode ser bloqueado —
    opt-out barra DISPARO, não conversa.
    """
    from app.api import cloud_corretora_a as cg

    tel = "+5500000000000"
    opt_out.registrar(sessao, tel, motivo="teste")
    sessao.commit()

    # a trava abre a própria sessão; aponta pra mesma do teste
    monkeypatch.setattr("app.db.session.SessionLocal", lambda: sessao)
    monkeypatch.setattr(cg, "_dry_run_ativo", lambda: True)

    class _Linha:
        slug = "teste"
        phone_id = "0"
        token_var = "X"

    r = cg.enviar_template_linha(_Linha(), tel, "Fulano", origem="carrossel")
    assert r["sent"] is False
    assert r.get("erro") == "opt_out"

    # humano: passa (cai no dry-run, que devolve ok=True)
    r2 = cg.enviar_template_linha(_Linha(), tel, "Fulano", origem=None)
    assert r2["ok"] is True
    assert r2["dry_run"] is True


def test_falha_do_banco_nao_para_a_regua(monkeypatch):
    """Falha ABERTA de propósito: banco fora não pode parar a operação.

    O risco aceito é uma mensagem a mais pra quem optou por sair; o risco
    recusado é a régua inteira parar calada por erro de conexão.
    """
    from app.api import cloud_corretora_a as cg

    def _explode():
        raise RuntimeError("banco fora")

    monkeypatch.setattr("app.db.session.SessionLocal", _explode)
    monkeypatch.setattr(cg, "_dry_run_ativo", lambda: True)

    class _Linha:
        slug = "teste"
        phone_id = "0"
        token_var = "X"

    r = cg.enviar_template_linha(_Linha(), "+5500000000000", "Fulano", origem="carrossel")
    assert r["ok"] is True  # passou, não travou
