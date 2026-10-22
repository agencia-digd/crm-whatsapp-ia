"""A segunda opinião do corretor no opt-out — ela sugere, a regra decide.

Medição que originou os cortes: `projetos/acme/RELATORIO-CORRETOR-OPTOUT.md`
(21/09/2026, 42.521 mensagens de 14 meses). Aqui não se mede nada de novo: o
que estes testes travam é o CONTRATO que a medição exigiu, e nenhum deles toca
a rede — o transporte é falso em todos.

O que não pode regredir, em ordem de dano:

  1. classificador fora do ar não apaga nem inventa descadastro (falha aberta);
  2. veto não apaga o opt-out que a regra gravou — vira aviso;
  3. rede não descadastra ninguém — vira aviso;
  4. placeholder (`[audio]`, `[reaction]`, `[unsupported]`, `''`) não vira nota
     e não vira linha nenhuma;
  5. o enunciado é o da medição, e os cortes são 0,50 e 0,85.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text as sql_text

from app.config import settings
from app.services import opt_out, opt_out_corretor as corretor


@pytest.fixture(autouse=True)
def _liga_a_flag(monkeypatch):
    """A flag nasce desligada em produção; aqui ela precisa estar ligada."""
    monkeypatch.setattr(settings, "opt_out_corretor_ativo", True)


def transporte_com(nota: float):
    """Transporte falso que sempre devolve `nota`, no formato real da API."""
    def _t(payload: dict) -> dict:
        assert payload["model"] == corretor.MODELO
        assert payload["questions"]["optout"]["instructions"] == corretor.ENUNCIADO
        return {"answers": {"optout": {"noul": nota}}}
    return _t


def transporte_fora_do_ar(payload: dict) -> dict:
    raise ConnectionError("sem rede")


# ── 5. O que a medição fixou ─────────────────────────────────────────────────

def test_enunciado_e_cortes_sao_os_da_medicao():
    """Se este teste falhar, a medição precisa ser REFEITA, não o teste ajustado.

    A nota é propriedade do enunciado: trocar "parar de receber mensagens" por
    "sair da lista" moveu a nota da mesma fala de 0,10 pra 0,82 (+0,72),
    enquanto repetir a mesma pergunta move 0,01 (relatório §6). Corte calibrado
    com uma redação não vale pra outra.
    """
    assert corretor.ENUNCIADO == "essa pessoa está pedindo para parar de receber mensagens?"
    assert corretor.CORTE_VETO == 0.50
    assert corretor.CORTE_REDE == 0.85
    assert corretor.MODELO == "typesafe/corretor-1.13"


# ── 1. Falha aberta ──────────────────────────────────────────────────────────

def test_api_fora_do_ar_a_regra_decide_sozinha():
    """Sem rede: a decisão da regra sai inteira, e ninguém vira conferência."""
    v = corretor.segunda_opiniao("Parar mensagens", transporte=transporte_fora_do_ar)
    assert v.decisao is True          # a regra marcou, e continua marcando
    assert v.corretor_rodou is False
    assert v.nota is None
    assert v.confere is False         # sem nota não existe aviso
    assert "ConnectionError" in v.motivo


def test_api_fora_do_ar_nao_inventa_descadastro():
    """A outra ponta: o que a regra NÃO marcou continua não marcado."""
    v = corretor.segunda_opiniao("bom dia, tudo bem?", transporte=transporte_fora_do_ar)
    assert v.decisao is False
    assert v.confere is False


def test_rodape_avisa_que_a_segunda_passada_nao_rodou():
    """Silêncio e "nada a conferir" não podem virar a mesma tela."""
    vs = [corretor.segunda_opiniao(t, transporte=transporte_fora_do_ar)
          for t in ("Parar mensagens", "bom dia")]
    assert "NÃO RODOU" in corretor.rodape(vs)
    # e quando roda, o rodapé conta o que houve
    vs_ok = [corretor.segunda_opiniao("Parar mensagens", transporte=transporte_com(0.77))]
    r = corretor.rodape(vs_ok)
    assert "NÃO RODOU" not in r
    assert "descadastra ninguém sozinho" in r


def test_resposta_fora_do_formato_tambem_e_falha_aberta():
    v = corretor.segunda_opiniao("Parar mensagens", transporte=lambda p: {"answers": {}})
    assert v.decisao is True
    assert v.corretor_rodou is False


def test_flag_desligada_nao_consulta(monkeypatch):
    """Com a flag off nem chamada sai — nem a chave é lida."""
    monkeypatch.setattr(settings, "opt_out_corretor_ativo", False)

    def _explode(payload):  # pragma: no cover — tem que NÃO ser chamado
        raise AssertionError("consultou o corretor com a flag desligada")

    v = corretor.segunda_opiniao("Parar mensagens", transporte=_explode)
    assert v.decisao is True
    assert v.corretor_rodou is False


# ── 2. O veto não apaga nada ─────────────────────────────────────────────────

def test_veto_nao_apaga_o_opt_out():
    """A regra marcou, o corretor discorda: a decisão fica de pé e vira AVISO.

    O caso real: "…enfim, então pode tirar da lista" — lista de UM
    empreendimento, a conversa segue e a pessoa agenda visita no sábado. A
    regra marca, o corretor dá 0,09. Ainda assim o opt-out continua valendo até
    alguém olhar: o veto é o lado frágil ao enunciado (sob outra redação ele
    perde 1 ou 2 dos 7 vetos), e frágil não decide.
    """
    v = corretor.segunda_opiniao("então pode tirar da lista", transporte=transporte_com(0.09))
    assert v.decisao is True          # ← o que vale, intocado
    assert v.tipo == corretor.VETO
    assert v.confere is True
    assert "discorda" in v.aviso
    assert "continua valendo" in v.aviso


def test_corretor_concordando_com_a_regra_nao_gera_linha():
    v = corretor.segunda_opiniao("Parar mensagens", transporte=transporte_com(0.77))
    assert v.decisao is True
    assert v.tipo is None
    assert v.confere is False


def test_corte_do_veto_e_fechado_embaixo():
    """0,50 exato NÃO é veto — abaixo dele, sim. A margem medida é 0,11
    (maior falso positivo 0,42 · menor opt-out real 0,53)."""
    assert corretor.segunda_opiniao("Parar mensagens", transporte=transporte_com(0.50)).tipo is None
    assert corretor.segunda_opiniao("Parar mensagens", transporte=transporte_com(0.49)).tipo == corretor.VETO


# ── 3. A rede só sugere ──────────────────────────────────────────────────────

def test_rede_sugere_e_nao_descadastra():
    """"Pode me tirar da sua lista por favor" — o "sua" quebra a regex da regra.

    É o único caso, em 14 meses, que o corretor pega e a regra perde com corte 0,85.
    Ele entra como linha de conferência; ninguém é descadastrado por isso.
    """
    fala = "Pode me tirar da sua lista por favor"
    assert opt_out.is_opt_out(fala) is False      # a regra de hoje perde este
    v = corretor.segunda_opiniao(fala, transporte=transporte_com(0.90))
    assert v.decisao is False                     # ← e continua perdendo: ele não decide
    assert v.tipo == corretor.REDE
    assert "Ninguém foi descadastrado" in v.aviso


def test_encerrar_interesse_nao_vira_rede():
    """O erro caro, e o que a rede em 0,85 existe pra não cometer.

    "não tenho mais interesse" tirou nota 0,70-0,82 na medição — 130 mensagens
    dessas no conjunto. Descadastrar quem disse isso mata lead vivo, que é o
    dano que o módulo documenta desde 26/07.
    """
    for nota in (0.70, 0.81, 0.84):
        v = corretor.segunda_opiniao("desculpe não tenho mais interesse..grato",
                                transporte=transporte_com(nota))
        assert v.decisao is False
        assert v.tipo is None, f"nota {nota} não pode virar sugestão"


# ── 4. Placeholder não vira nada ─────────────────────────────────────────────

@pytest.mark.parametrize("entrada", ["", "   ", "[audio]", "[reaction]",
                                     "[unsupported]", "[revoke]", "[sticker]"])
def test_placeholder_nao_consulta_e_nao_sugere(entrada):
    """Nem com nota alta forjada: a defesa é estrutural e vem antes da API.

    Na DEX (20/09) `[unsupported]` tirou 0,77 e dez leads entraram numa lista
    por causa disso. Aqui o transporte nem é chamado — se for, o teste quebra.
    """
    def _explode(payload):  # pragma: no cover — tem que NÃO ser chamado
        raise AssertionError(f"consultou o corretor pra {entrada!r}")

    v = corretor.segunda_opiniao(entrada, transporte=_explode)
    assert v.decisao is False
    assert v.nota is None
    assert v.confere is False
    assert v.motivo == "sem conteúdo"


def test_sem_conteudo_distingue_fala_de_placeholder():
    assert corretor.sem_conteudo("[audio]") is True
    assert corretor.sem_conteudo(None) is True
    # fala de gente com colchete no meio não é placeholder
    assert corretor.sem_conteudo("para com isso") is False
    assert corretor.sem_conteudo("[bom dia]") is False   # tem espaço: é fala


# ── A gravação: escreve aviso, e só aviso ────────────────────────────────────

@pytest.fixture()
def sessao():
    from app.db.session import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


def _conferencias(sessao, telefone: str) -> list[tuple]:
    return sessao.execute(
        sql_text("SELECT tipo, regra_marcou, nota, enunciado FROM opt_out_conferencia "
                 "WHERE telefone = :t"), {"t": telefone},
    ).all()


def test_veto_gravado_nao_desfaz_o_opt_out_do_banco(sessao):
    """A prova que importa: com o veto na mesa, a pessoa segue bloqueada.

    Se algum dia alguém fizer o veto "resolver sozinho", este teste cai.
    """
    tel = "+5500000000000"
    opt_out.registrar(sessao, tel, motivo="teste")
    sessao.flush()
    assert opt_out.esta_bloqueado(sessao, tel) is True

    v = corretor.segunda_opiniao("então pode tirar da lista", transporte=transporte_com(0.09))
    assert corretor.registrar_conferencia(sessao, v, telefone=tel, texto="então pode tirar da lista")
    sessao.flush()

    # a linha de aviso existe…
    linhas = _conferencias(sessao, tel)
    assert len(linhas) == 1
    assert linhas[0][0] == corretor.VETO
    assert linhas[0][1] is True                 # regra_marcou
    assert linhas[0][3] == corretor.ENUNCIADO        # com que pergunta aquela nota saiu
    # …e o opt-out continua de pé
    assert opt_out.esta_bloqueado(sessao, tel) is True
    assert sessao.execute(
        sql_text("SELECT status FROM cadencia_estado "
                 "WHERE right(regexp_replace(telefone,'\\D','','g'),8) = :s"),
        {"s": "99990101"},
    ).scalar() == "opt_out"


def test_rede_gravada_nao_bloqueia_ninguem(sessao):
    """Sugestão vira linha na fila de conferência e MAIS NADA."""
    tel = "+5500000000000"
    v = corretor.segunda_opiniao("Pode me tirar da sua lista por favor",
                            transporte=transporte_com(0.90))
    assert corretor.registrar_conferencia(sessao, v, telefone=tel, texto="…") is True
    sessao.flush()

    assert _conferencias(sessao, tel)[0][0] == corretor.REDE
    # ninguém foi descadastrado: a fila de envio continua enxergando a pessoa
    assert opt_out.esta_bloqueado(sessao, tel) is False


def test_concordancia_nao_grava_linha(sessao):
    tel = "+5500000000000"
    v = corretor.segunda_opiniao("Parar mensagens", transporte=transporte_com(0.77))
    assert corretor.registrar_conferencia(sessao, v, telefone=tel, texto="Parar mensagens") is False
    sessao.flush()
    assert _conferencias(sessao, tel) == []


def test_gravar_duas_vezes_a_mesma_mensagem_nao_duplica(sessao):
    """Varredura que roda de novo não vira duas linhas iguais na fila."""
    import uuid

    tel = "+5500000000000"
    msg = uuid.uuid4()
    v = corretor.segunda_opiniao("Pode me tirar da sua lista por favor",
                            transporte=transporte_com(0.90))
    assert corretor.registrar_conferencia(sessao, v, telefone=tel, mensagem_id=msg, texto="x") is True
    sessao.flush()
    assert corretor.registrar_conferencia(sessao, v, telefone=tel, mensagem_id=msg, texto="x") is False
    sessao.flush()
    assert len(_conferencias(sessao, tel)) == 1
