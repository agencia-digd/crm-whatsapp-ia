"""Ampliação dos detectores de resposta do remarketing (Dona, 05/10/2026).

Quatro grupos que ficavam `indefinida` e ganharam leitura:

    negativa curta   "Não", "Não estou", "Não mais"        → recusa
    já resolveu      "Já compramos", "Achei", "Comprei um" → recusa
    pedido não lido  "Olá, estou sim", "manda por favor"   → interesse
    número errado    "esse contato não é mais da Fulana"   → numero_errado

O que estes testes seguram, além de "a frase casa":

1. **Só refina indefinida.** Nenhuma frase que os detectores da casa já leem
   muda de classe — é a garantia de que a Pessoa e o handoff não foram mexidos.
2. **O que depende da pergunta só vale como resposta direta.** "Não" no meio
   de uma conversa continua indefinida.
3. **Nenhuma frase de gente interessada vira recusa.** As frases de
   `INTERESSADOS_QUE_A_MEDICAO_PEGOU` são reais: a primeira versão das regras
   mandava cada uma pra Venda perdida, e a medição no corpo de 37 mil
   mensagens pegou (`projetos/crm/docs/2026-10-05-garantia-remarketing/`).

As frases são as reais, com nome de pessoa trocado.
"""
from __future__ import annotations

import pytest

from app.services import remarketing_resposta as rr


def c(texto: str, direta: bool, seg: float = 120.0) -> tuple[str, str]:
    return rr.classificar_com_regra(texto, "text", seg, resposta_direta=direta)


# ── 1. Negativa curta — só como resposta direta ─────────────────────────────

NEGATIVAS = [
    "Não", "Nao", "não.", "N", "Não estou", "Não mais", "Eu não", "Não não",
    "Bom dia, não estou, obrigado", "Boa tarde Não estamos mais, obrigado.",
    "Olá, não mais!", "Olá!! Não estou🙏🏻", "Boa tarde! Não estou não.",
    "Obg mas não mais", "Não. Obrigada", "Ola boa tarde Não mais, obrigada",
]


@pytest.mark.parametrize("frase", NEGATIVAS)
def test_negativa_curta_como_resposta_direta_e_recusa(frase):
    assert c(frase, True) == (rr.RECUSA, rr.REGRA_NEGATIVA_CURTA)


@pytest.mark.parametrize("frase", NEGATIVAS)
def test_negativa_curta_no_meio_da_conversa_continua_indefinida(frase):
    """A regra da Dona: "Não" isolado só é recusa como resposta direta a um
    disparo, nunca no meio de conversa com corretora."""
    assert c(frase, False)[0] == rr.INDEFINIDA


@pytest.mark.parametrize("frase", [
    "Não estou bem",            # está falando de si
    "ainda não",                # ainda não achou? ainda não quer?
    "Obrigada, mas ainda não",
    "não sei",
    "Não, mas",                 # frase que continua na próxima mensagem
    "Não entendi",
    "Não lembro de ter pedido isso mas pode falar",
])
def test_nao_que_nao_e_recusa_fica_indefinida(frase):
    assert c(frase, True)[0] != rr.RECUSA


# ── 2. Já resolveu ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("frase", [
    "Já compramos obrigada", "Oii obrigado, mas já compramos🥰", "Achei",
    "Eu comprei um obrigado", "Comprei faz 3 anos já 😳", "Consegui",
    "Obrigado, já estou morando aqui em Cidade.", "Já fechamos um imóvel",
])
def test_ja_resolveu_como_resposta_direta_e_recusa(frase):
    assert c(frase, True) == (rr.RECUSA, rr.REGRA_JA_RESOLVEU)


@pytest.mark.parametrize("frase", ["Achei", "Consegui", "Comprei em Londrina",
                                   "Eu tenho ele a muito tempo. Comprei em Londrina"])
def test_ja_resolveu_no_meio_da_conversa_nao_encerra(frase):
    """No meio da conversa "consegui" é "consegui mandar o documento" e
    "comprei em Londrina" é a pessoa contando do imóvel que tem."""
    assert c(frase, False)[0] != rr.RECUSA


# ── 3. Pedido e sim que a regra não lia ─────────────────────────────────────

@pytest.mark.parametrize("frase", [
    "Boa tarde manda por favor", "Favor mandar", "Boa tarde manda aí",
    "Boa tarde poderia me mandar", "Me manda", "Mande",
    "Olá, me mande pra eu dar uma olhada só",
])
def test_pedido_e_interesse_em_qualquer_mensagem(frase):
    assert c(frase, False) == (rr.INTERESSE, rr.REGRA_PEDIDO)


@pytest.mark.parametrize("frase", [
    "Vou mandar", "Vou te mandar", "Ok, vou enviar e já te aviso",
    "Provavelmente ele manda hoje pra mim", "Pede ele pra mandar um áudio explicando",
    "Quer enviar o contato do gestor da imob ?",
])
def test_ter_o_verbo_mandar_nao_e_pedir(frase):
    """A primeira versão casava qualquer "manda/envia". Quem diz "vou mandar"
    não está pedindo nada — e interesse lido errado vira card de corretora."""
    assert c(frase, True)[1] != rr.REGRA_PEDIDO


@pytest.mark.parametrize("frase", [
    "Olá, estou sim", "Oi boa tarde pode sim", "Siimmm", "Oi, boa tarde, ainda estou",
    "Oi. Boa tarde. Sim desejo. Agradeço", "Bom dia Sim por favor", "Queremos sim",
])
def test_sim_depois_do_cumprimento_e_interesse_como_resposta_direta(frase):
    assert c(frase, True) == (rr.INTERESSE, rr.REGRA_SIM_DIRETO)


@pytest.mark.parametrize("frase", [
    "Olá, estou sim", "Oi boa tarde pode sim", "Siimmm",
])
def test_sim_no_meio_da_conversa_nao_e_lido_por_esta_regra(frase):
    assert c(frase, False)[1] != rr.REGRA_SIM_DIRETO


@pytest.mark.parametrize("frase", [
    "Oi boa tarde! Estou bem e vc?", "Tudo bem sim e você?", "Bom dia tudo bem sim",
    "Ah sim", "Ah sim entendi",
])
def test_resposta_ao_cumprimento_nao_e_sim_ao_disparo(frase):
    assert c(frase, True)[1] != rr.REGRA_SIM_DIRETO


# ── 4. Número errado ────────────────────────────────────────────────────────

@pytest.mark.parametrize("frase", [
    "Oi esse contato não é mais da Fulana", "Este fone é do Beltrano",
    "Olá infelizmente este número não é do Sicrano", "Número errado",
    "Moça, pelo jeito ela n queria MT os imóveis n, te passou o número errado 🤣",
    "Você se enganou e mandou msg para pessoa errada.",
    "Desculpe vc ligou pra pessoa errada", "Acho que passaram o contato errado",
])
def test_numero_errado_dito_com_todas_as_letras_vale_sempre(frase):
    for direta in (True, False):
        assert c(frase, direta) == (rr.NUMERO_ERRADO, rr.REGRA_NUMERO_ERRADO)


@pytest.mark.parametrize("frase", [
    "Não conheço Beltrano", "Não conheço", "Foi engano amiga",
    "Oi boa tarde, mensagem errada", "Boa tarde , não conheço",
])
def test_numero_errado_fraco_so_como_resposta_direta(frase):
    assert c(frase, True) == (rr.NUMERO_ERRADO, rr.REGRA_NUMERO_ERRADO)
    assert c(frase, False)[0] != rr.NUMERO_ERRADO


def test_pergunta_nao_e_afirmacao():
    """"Esse número é da Pessoa1?" é a pessoa conferindo com quem fala."""
    assert c("Esse numero é da Pessoa1 ?", True)[0] != rr.NUMERO_ERRADO


# ── 5. A prova que a ordem pediu: interessado não vira recusa ───────────────

INTERESSADOS_QUE_A_MEDICAO_PEGOU = [
    # a 1ª versão lia "não conheço" como número errado
    "Não conheço os bairros",
    "Nao conheço os bairros",
    "Não conheço muito aqui",
    "Eu sou de Jundiaí, não conheço muito bem cidade",
    "Santa Rosália eu não conheço vou colocar aqui no GPS",
    "Não conheço..é no Bairro A?",
    "Eu cheguei aqui ontem a noite. Não conheço nada",
    "não conheço ainda",
    "Não conheço essa cidade",
    # "foi engano" e "mensagem errada" de quem se desculpa da própria mensagem
    "Oh desculpa, foi engano",
    "Não mensagem errada desculpa",
    "Não acredite em mim, eu sou uma pessoa errada, tentando ser feliz",
]


@pytest.mark.parametrize("frase", INTERESSADOS_QUE_A_MEDICAO_PEGOU)
def test_frase_de_gente_que_conversa_nao_vai_pra_venda_perdida(frase):
    """No meio de uma conversa (onde estas frases aparecem), nenhuma pode
    virar recusa nem número errado."""
    assert c(frase, False)[0] not in rr.CLASSES_VENDA_PERDIDA


@pytest.mark.parametrize("frase", [f for f in INTERESSADOS_QUE_A_MEDICAO_PEGOU
                                   if "conhe" in f and f != "Não conheço"])
def test_nao_conheco_o_lugar_nao_e_numero_errado_nem_como_resposta_direta(frase):
    assert c(frase, True)[0] not in rr.CLASSES_VENDA_PERDIDA


# ── 6. Só refina indefinida: o que a casa já lia continua igual ─────────────

JA_LIDAS = [
    ("quero ver sim", rr.INTERESSE),
    ("pode mandar", rr.INTERESSE),
    ("Comprei um terreno de vcs ...preciso do número da imobiliaria", rr.INTERESSE),
    ("Não quero apartamento, quero casa", rr.INTERESSE),
    ("Não quero", rr.RECUSA),
    ("No momento não", rr.RECUSA),
    ("Já comprei, obrigada", rr.RECUSA),
    ("Não tenho interesse", rr.RECUSA),
    ("parar", rr.PARAR),
    ("Não manda mais", rr.PARAR),
    ("Loja Exemplo agradece seu contato. Como podemos ajudar?", rr.AUTOMATICA),
]


@pytest.mark.parametrize("frase,classe", JA_LIDAS)
@pytest.mark.parametrize("direta", [True, False])
def test_o_que_ja_era_lido_nao_muda_e_nao_passa_por_regra_nova(frase, classe, direta):
    cl, regra = c(frase, direta)
    assert cl == classe
    assert regra not in rr.REGRAS_AMPLIADAS


@pytest.mark.parametrize("frase", NEGATIVAS + ["Achei", "Olá, estou sim", "Não conheço"])
def test_sem_resposta_direta_a_regra_e_a_de_antes(frase):
    """`classificar_resposta` chamada do jeito antigo (sem dizer que é
    resposta direta) devolve o que devolvia antes de 05/10."""
    assert rr.classificar_resposta(frase, "text", 120.0) == rr.INDEFINIDA


def test_relogio_e_parar_continuam_ganhando_das_regras_novas():
    assert c("Não", True, seg=2.0) == (rr.AUTOMATICA, rr.REGRA_RELOGIO)
    assert c("não me mande mais", True)[0] == rr.PARAR


def test_numero_errado_fecha_como_venda_perdida():
    assert rr.NUMERO_ERRADO in rr.CLASSES_VENDA_PERDIDA
    assert rr.NUMERO_ERRADO not in rr.CLASSES_QUE_CONTAM
