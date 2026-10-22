"""Obra 10 (22/10/2026) — desinteresse claro vira opt-out; ambíguo NÃO.

Fonte das frases: lista DESINTERESSE do DEX (`crm-dex/lib/fup.ts`) + 30
mensagens inbound REAIS da Acme (SELECT somente leitura em
`mensagens_whatsapp`, 22/10; sem telefone, sem nome de pessoa).

Regra de ouro dos negativos: falso positivo = 0. Opt-out errado mata um lead.
"""
from __future__ import annotations

import pytest

from app.services.opt_out import classificar_desinteresse, motivo_opt_out
from app.services.opt_out import is_opt_out as _is_pedido_de_parar


def is_opt_out(texto):
    """O que a RÉGUA decide: pedido de parar OU desinteresse claro."""
    return motivo_opt_out(texto) is not None

# Frases do DEX que viram opt-out aqui (mensagem inteira, com ou sem cortesia).
DEX_SIM = [
    "encerrar atendimento",
    "Pode encerrar",
    "não tenho mais interesse",
    "Não tô mais interessado",
    "não estou mais procurando",
    "não estamos mais procurando",
    "nao tenho interesse",
    "Não quero mais",
    "sem interesse",
    "Parar de mandar mensagem",      # DEX: pare de mandar
    "pare de enviar",
    "descadastrar",
    "remover",
]

# 30 mensagens reais — as que são desinteresse claro.
REAIS_SIM = [
    "Não tenho mais interesse obrigado",
    "Nao tenho interesse.",
    "Olá não tenho interesse, Obrigada",
    "Não temos mais interesse em imóvel",
    "Não tenho mais interesse no imóvel",
    "Boa tarde, não tenho interesse Agradeço pelo contato",
    "Boa tarde! Não estou procurando! Obrigada!",
    "Não estou procurando",
    "Pode encerrar",
]

# Real, é desinteresse, mas escrito com "mais" no lugar de "mas": a cortesia não
# casa e a mensagem NÃO vira opt-out. Falso negativo aceito (custa 1 mensagem).
REAL_FALSO_NEGATIVO_ACEITO = [
    "Bom dia, tudo bem?   Nao tenho mais interesse,  mais agradeço  a atenção 🙏🏻",
]

# Reais e ambíguos/outros: NENHUM pode virar opt-out.
REAIS_NAO = [
    "Agradeço o contato, mas no momento não tenho interesse",
    "Bom dia! No momento não estou interessado.  Obrigado!",
    "Oi boa noite no momento não estou procurando",
    "Não estou procurando no momento",
    "Agora não",
    "Agora não dá obrigada",
    "Ok Obrigado  Por enquanto nao",
    "Não posso falar agora. Manda um SMS, por favor?",
    "Nao posso falar agora",
    "Desculpa nao e esse",
    "Essa região nao tenho interesse..",
    "Sim...na região do Vitória região não tenho interesse",
    "Não estou procurando  Estou vendendo um terreno",
    "Olá tudo bem? Não estou mais procurando. Porém se puder me mandar, vou avaliar ok?",
    "E não quero esse pichain aq não",
    "Os da vila Barão eu não quero.",
    "Boa tarde… ainda não. Vou suspender a procura por enquanto.",
    "Desisti por um tempo",
    "Muitíssimo obrigado, consegui fazer um ótimo negócio, no momento não estou interessado. Bons negócios🙏",
    "Achei interessante, mas parece que é de escada né",
]

# As ambíguas que a Dona listou, escritas do jeito que a pessoa escreveria.
AMBIGUAS = [
    "não agora", "Não agora, obrigado", "agora não", "não é esse", "não é esse não",
    "depois eu vejo", "Depois eu vejo isso", "não posso falar agora",
    "não posso falar agora, me chama mais tarde", "não quero esse bairro",
    "não quero mais caro que 300 mil", "não tenho interesse nessa região",
    "talvez mais pra frente", "por enquanto não", "no momento não tenho interesse",
    "não tenho interesse agora", "não estou procurando agora",
    "ainda não tenho interesse", "não preciso agora",
]

# "Já comprei/resolvi" tem fluxo próprio (despedida + indicação): não é opt-out.
JA_ENCONTROU = [
    "Eu já comprei", "Já comprei, obrigado", "ja resolvi", "Já fechei em outro empreendimento",
    "Olá, não preciso mais, comprei!",
]


@pytest.mark.parametrize("texto", DEX_SIM + REAIS_SIM)
def test_desinteresse_claro_vira_opt_out(texto):
    assert is_opt_out(texto), texto


@pytest.mark.parametrize("texto", REAIS_NAO + AMBIGUAS + JA_ENCONTROU)
def test_ambiguo_e_outras_negativas_nao_viram_opt_out(texto):
    assert not is_opt_out(texto), texto


def test_falsos_positivos_zero_nas_negativas():
    achados = [t for t in REAIS_NAO + AMBIGUAS + JA_ENCONTROU if is_opt_out(t)]
    assert achados == []


def test_falso_negativo_aceito_nao_vira_opt_out():
    assert not any(is_opt_out(t) for t in REAL_FALSO_NEGATIVO_ACEITO)


def test_trinta_mensagens_reais():
    assert len(REAIS_SIM) + len(REAL_FALSO_NEGATIVO_ACEITO) + len(REAIS_NAO) == 30


def test_motivo_distingue_pedido_de_desinteresse():
    assert motivo_opt_out("parar") == "pedido"
    assert motivo_opt_out("pare de mandar mensagem") == "pedido"
    assert motivo_opt_out("Não tenho mais interesse obrigado") == "desinteresse"
    assert motivo_opt_out("agora não") is None
    assert motivo_opt_out(None) is None


def test_frase_de_auditoria_e_a_frase_normalizada_sem_cortesia():
    assert classificar_desinteresse("Olá, NÃO tenho interesse! Obrigada") == "nao tenho interesse"
    assert classificar_desinteresse("no momento não tenho interesse") is None
    assert classificar_desinteresse("") is None


def test_is_opt_out_canonico_nao_mudou():
    """Remarketing/Corretor/saída seguem tratando 'não tenho interesse' como recusa
    branda: o desinteresse só vale na resposta inbound da cadência."""
    assert not _is_pedido_de_parar("Não tenho mais interesse obrigado")
    assert _is_pedido_de_parar("parar")
