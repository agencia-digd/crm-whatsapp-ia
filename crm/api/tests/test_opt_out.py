"""Opt-out canônico — regressão do bug que matou 3 leads em produção.

Auditoria 26/07/2026: `cadencia.is_opt_out` era `"para" in texto`, então a
preposição "para" marcava o lead como descadastrado E o movia pra Venda perdida.
Os 3 únicos opt-outs do banco de produção eram falsos positivos por causa disso.

Os casos NÃO-opt-out abaixo com marca [PROD] são textos REAIS que dispararam o
bug. Se algum voltar a dar True, o bug voltou.
"""
from __future__ import annotations

import pytest

from app.api.cloud_corretora_a import _is_opt_out
from app.services.cadencia import is_opt_out as is_opt_out_cadencia
from app.services.opt_out import is_opt_out

NAO_OPT_OUT = [
    # [PROD] textos reais que causaram falso positivo
    "Olá, tudo bem? Que prazer enorme tê-lo por aqui! 😊✨ Estamos ansiosos para atendê-lo",
    "Com muita alegria, convidamos você para a inauguração do nosso novo ateliê!",
    # cenários de imobiliária que NÃO são descadastro
    "quero um apê para minha mãe, 2 quartos",
    "é para investir mesmo",
    "preciso cancelar a visita de amanhã, pode ser quinta?",
    "vamos ter que cancelar hoje, surgiu um imprevisto no trabalho",
    "não quero esse bairro, tem algo mais perto do centro?",
    "não quero mais caro que 300 mil, dá pra achar?",
    "não quero financiar, vou pagar à vista",
    "vou sair de casa agora e te chamo quando chegar",
    "pode me mandar as fotos para o meu email?",
    "estou procurando um terreno para construir",
    "",
    None,
]

OPT_OUT = [
    "Parar mensagens",          # Quick Reply dos templates UTILITY
    "parar",
    "PARE!",
    "pare",
    "stop",
    "sair",
    "cancelar",                 # resposta seca curta
    "quero descadastrar",
    "descadastrar",
    "não quero receber mais mensagens",
    "nao quero receber essas mensagens",
    "pare de mandar mensagem por favor",
    "para de enviar mensagem",
    "não me manda mais mensagem",
    "remover meu número da lista",
    "me tira da lista",
    "não perturbe mais",
    "não quero mais",          # recusa global em msg curta (era o caso do teste da cadência)
    "não quero mais, para",
    "não quero nada",
]


@pytest.mark.parametrize("texto", NAO_OPT_OUT)
def test_nao_marca_opt_out(texto):
    assert is_opt_out(texto) is False


@pytest.mark.parametrize("texto", OPT_OUT)
def test_marca_opt_out(texto):
    assert is_opt_out(texto) is True


@pytest.mark.parametrize("texto", ["Estamos ansiosos para atendê-lo", "Parar mensagens"])
def test_implementacao_unica_nos_tres_pontos_de_uso(texto):
    """cadência, reengajamento da Pessoa12 e painel precisam concordar — eram 2 regras
    divergentes antes da unificação (uma marcava, a outra não)."""
    assert is_opt_out(texto) == is_opt_out_cadencia(texto) == _is_opt_out(texto)


def test_palavra_comando_dentro_de_conversa_longa_nao_marca():
    """"cancelar"/"sair" só valem em resposta CURTA — no meio de uma conversa
    real são outra coisa (cancelar visita, sair de casa)."""
    assert is_opt_out("cancelar") is True
    assert is_opt_out("vou ter que cancelar a visita porque meu filho ficou doente") is False


# ── regressões de 20/08/2026 ─────────────────────────────────────────────────
# A Flavia pediu duas vezes pra parar e recebeu disparo mesmo assim. A causa
# foi boba: a regex tinha "para" e "pare", não tinha "parar".

def test_parar_de_receber_com_o_verbo_inteiro():
    """O caso da Flavia, letra por letra (inclusive o typo dela)."""
    assert is_opt_out("Não, eu ja comprei o meu imovel.\n\nGosgaria de parar "
                      "de receber mensagens, por favor. Assim como ja tinha "
                      "deixado claro anteriormente")


@pytest.mark.parametrize("texto", [
    "gostaria de parar de receber mensagens",
    "quero parar de receber",
    "favor parar",
    "não desejo receber mensagens",
    "não envie mais nada por favor",
    "me exclua da sua lista",
    "Poderia tirar meu número do registro de vocês por favor",
    "Como eu faço pra vc não me mandar mensagem mais?",
    "Parar mensagens",          # texto do botão dos templates novos
    "PARAR DE RECEBER",         # texto do botão antigo
])
def test_pedidos_reais_de_saida(texto):
    assert is_opt_out(texto)


@pytest.mark.parametrize("texto", [
    # Achados na varredura: falsos positivos que a correção NÃO pode reintroduzir
    "Estou com um cliente aqui para mandar contrato já",
    "te chamo para mandar os documentos",
    "mas não me mandaram nenhuma proposta ainda",
    "Ok. Vou tentar pelo app hoje, se não conseguir peço para o RH amanhã cedo",
    "não vou sair",
    "Vai sair",
    "Cancela amanhã",
    "preciso cancelar a visita de amanhã",
])
def test_conversa_normal_nao_e_optout(texto):
    """Falso positivo aqui mata um lead vivo — erramos pro lado barato."""
    assert not is_opt_out(texto)


def test_comando_ambiguo_so_vale_sozinho():
    assert is_opt_out("sair") and is_opt_out("cancelar")
    assert not is_opt_out("vai sair amanhã") and not is_opt_out("cancela a visita")
