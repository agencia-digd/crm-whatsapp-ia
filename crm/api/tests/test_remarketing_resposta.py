"""A classe da resposta ao remarketing — função pura, sem banco e sem rede.

As frases são respostas REAIS ao carrossel da Corretor (30/07 a 09/09/2026), tiradas
do levantamento em `projetos/crm/docs/2026-10-05-respostas-remarketing/`. O que
foi trocado: nome de pessoa e de empresa. Os segundos são os medidos.

O que estes testes protegem:
1. Cada classe cai onde a Dona decidiu (05/10/2026).
2. A ORDEM: parar ganha de tudo, botão ganha do relógio, o relógio ganha do
   texto, e recusa ganha do detector de mensagem automática.
3. Os casos de borda do pedido: recusa seguida de interesse, "não" isolado,
   resposta em 3 s com conteúdo de gente, automática em 40 s.
"""
from __future__ import annotations

import pytest

from app.services import remarketing_resposta as rr
from app.services.cadencia import _e_auto_reply_conteudo
from app.services.remarketing_resposta import classificar_resposta as classificar

# (texto, tipo, segundos entre o disparo e a resposta)
INTERESSE = [
    ("pode enviar por favor", "text", 24.2),
    ("Boa tarde! Tudo bem e você?  Sim, estou procurando", "text", 27.8),
    ("Ola, eu tenho interesse mas na zona oeste", "text", 29.5),
    ("Olá estou vendendo meu apto", "text", 34.1),
    ("Boa tarde, pra investir", "text", 42.9),
    ("Morar,no caso alugar", "text", 46.1),
    ("Quero", "text", 60.8),
    ("Sim", "text", 336.2),
    ("Estou sim", "text", 1371.8),
    ("Bom dia , tem algum casa dois dormitórios na região da vila Santana ?", "text", 208.0),
    ("Pode mandar , mas gostaria de apartamentos no bairro jardim Europa", "text", 1415.6),
    # recusa e venda na mesma frase: é captação, como no handoff
    ("Boa tarde, no momento não, primeiro preciso vender minha casa.", "text", 8300.9),
]

BOTAO = [
    ("SAIBA MAIS", "button", 16.4),
    ("SAIBA MAIS", "button", 22.4),
    ("Saiba mais", "interactive", 20126.0),
]

RECUSA = [
    ("Não estou mais obrigada", "text", 31.6),
    ("Obrigada,  ja aluguei", "text", 36.9),
    ("Boa tarde. Já encontrei. Muito obrigada.", "text", 38.7),
    ("Boa tarde no momento nao obrigado", "text", 45.2),
    ("Boa tarde....já adquiri um imóvel... agradeço sua atenção.", "text", 47.4),
    ("Não, obrigado.", "text", 53.4),
    ("Olá boa tarde, não preciso mais, comprei! Mas agradeço o contato.", "text", 60.0),
    ("Olá, tudo bem. Por hora, não penso em adquirir imóvel não.", "text", 77.2),
    ("Boa tarde, não tenho interesse Agradeço pelo contato", "text", 690.2),
    ("Olá. Desisti da compra", "text", 116.7),
    # o detector de automática casa "no momento não" — recusa ganha dele
    ("No momento não", "text", 340.6),
    ("Agradeço o contato, mas no momento não tenho interesse", "text", 91177.9),
]

PARAR = [
    ("PARAR DE RECEBER", "button", 334.8),
    ("Parar mensagens", "button", 1449.7),
    ("parar", "text", 90.0),
    ("gostaria de parar de receber mensagens, por favor", "text", 400.0),
    ("me tira da lista", "text", 61.0),
]

AUTOMATICA_PELO_RELOGIO = [
    ("Olá, seja bem vindo!", "text", 1.0),
    ("Agradeço seu contato. Como podemos ajudar?", "text", 2.6),
    ("Corretora Fulana tudo bem ?  Como posso te auxiliar ?", "text", 2.7),
    ("Oi, Beltrano aqui! Tudo bem com você? Para agilizar nosso atendimento, "
     "por favor, envie fotos e vídeos do reparo", "text", 3.4),
    ("Olá, para um orçamento precisamos saber seu curso se é TCC ou Artigo", "text", 4.5),
    ("Obrigada pela sua mensagem!  No momento estamos fora do nosso horário de "
     "atendimento", "text", 4.7),
]

AUTOMATICA_PELO_CONTEUDO = [
    ("Loja Exemplo agradece seu contato. Como podemos ajudar?", "text", 5.1),
    ("O Eletrônica Exemplo agradece seu contato. Como podemos ajudar?", "text", 11.8),
    ("A Casa dos Exemplos agradece seu contato. Como podemos ajudar?", "text", 13.5),
    ("Exemplo Automation agradece seu contato. Como podemos ajudar?", "text", 27.6),
    ("Olá! Seja bem-vindo(a) à Fulano Advocacia.  Agradecemos pelo seu contato.", "text", 5.0),
    ("Olá! Bem vindo ao WhatsApp do Financeiro da Paróquia Exemplo, aguarde um "
     "momento e será atendido (a)", "text", 5.0),
    ("Olá! 👋  Obrigado por entrar em contato com a Exemplo Decorações.", "text", 6.6),
    ("Drª Fulana agradece seu contato. No momento estou ocupada, em breve lhe "
     "retorno. Obrigada!", "text", 277.6),
    ("A Exemplo Cursos agradece seu contato, em alguns minutos você será "
     "respondido. Como podemos ajudar ?", "text", 390.4),
    ("Olá! Seja bem-vinda(o) à Exemplo Gourmet. ✨ Que bom ter você aqui!", "text", 2738.3),
]

INDEFINIDA = [
    ("Bom dia", "text", 10.5),
    ("Ola boa tarde", "text", 16.7),
    ("Oiii", "text", 20.2),
    ("Olá", "text", 28.8),
    ("Obg", "text", 36.3),
    ("🤔", "text", 68.9),
    # (número errado dito com todas as letras deixou de ser indefinida em
    # 05/10 — ver tests/test_remarketing_resposta_ampliada.py)
    ("Não conheço Beltrano", "text", 62.9),
    ("[audio]", "audio", 44.5),
    ("[sticker]", "sticker", 71938.3),
    ("[reaction]", "reaction", 37.2),
    ("[image]", "image", 89817.9),
    # negativa curta SEM saber que é resposta direta ao disparo (é como esta
    # função é chamada aqui): continua indefinida. Como resposta direta virou
    # recusa em 05/10 — tests/test_remarketing_resposta_ampliada.py.
    ("Não", "text", 10.7),
    ("Não estou", "text", 22.0),
    ("Olá, não mais!", "text", 39.1),
]


def _ids(casos):
    return [f"{seg:g}s:{texto[:32]}" for texto, _tipo, seg in casos]


@pytest.mark.parametrize("texto,tipo,seg", INTERESSE, ids=_ids(INTERESSE))
def test_interesse(texto, tipo, seg):
    assert classificar(texto, tipo, seg) == rr.INTERESSE


@pytest.mark.parametrize("texto,tipo,seg", BOTAO, ids=_ids(BOTAO))
def test_clique_no_botao(texto, tipo, seg):
    assert classificar(texto, tipo, seg) == rr.BOTAO


@pytest.mark.parametrize("texto,tipo,seg", RECUSA, ids=_ids(RECUSA))
def test_recusa(texto, tipo, seg):
    assert classificar(texto, tipo, seg) == rr.RECUSA


@pytest.mark.parametrize("texto,tipo,seg", PARAR, ids=_ids(PARAR))
def test_parar_por_texto_ou_botao(texto, tipo, seg):
    assert classificar(texto, tipo, seg) == rr.PARAR


@pytest.mark.parametrize("texto,tipo,seg", AUTOMATICA_PELO_RELOGIO,
                         ids=_ids(AUTOMATICA_PELO_RELOGIO))
def test_automatica_pelo_relogio(texto, tipo, seg):
    assert classificar(texto, tipo, seg) == rr.AUTOMATICA


@pytest.mark.parametrize("texto,tipo,seg", AUTOMATICA_PELO_CONTEUDO,
                         ids=_ids(AUTOMATICA_PELO_CONTEUDO))
def test_automatica_pelo_conteudo_depois_do_corte(texto, tipo, seg):
    assert seg >= rr.CORTE_PADRAO_S
    assert classificar(texto, tipo, seg) == rr.AUTOMATICA


@pytest.mark.parametrize("texto,tipo,seg", INDEFINIDA, ids=_ids(INDEFINIDA))
def test_indefinida(texto, tipo, seg):
    assert classificar(texto, tipo, seg) == rr.INDEFINIDA


# ── Casos de borda do pedido ────────────────────────────────────────────────

def test_nao_isolado_sem_saber_que_e_resposta_direta_e_indefinida():
    """"Não" sozinho não casa detector nenhum de recusa. Mandar pra Venda
    perdida é saída definitiva da roda — não se faz por palpite."""
    assert classificar("Não", "text", 10.7) == rr.INDEFINIDA
    assert classificar("Eu não", "text", 6772.1) == rr.INDEFINIDA


def test_resposta_em_3s_com_conteudo_de_gente_e_automatica():
    """Decisão da Dona: até o corte é o aparelho, seja qual for o texto. Nas
    395 respostas medidas ninguém respondeu em menos de 10,5 s."""
    assert classificar("Quero sim, pode mandar", "text", 3.0) == rr.AUTOMATICA
    assert classificar("Não tenho interesse", "text", 3.0) == rr.AUTOMATICA
    # e a mesma frase, depois do corte, é o que diz ser
    assert classificar("Quero sim, pode mandar", "text", 30.0) == rr.INTERESSE
    assert classificar("Não tenho interesse", "text", 30.0) == rr.RECUSA


def test_automatica_em_40s_e_pega_pelo_conteudo():
    assert classificar(
        "Loja Exemplo agradece seu contato. Como podemos ajudar?", "text", 40.0
    ) == rr.AUTOMATICA
    assert classificar(
        "Olá! Estamos ausentes no momento, retornaremos em breve.", "text", 40.0
    ) == rr.AUTOMATICA


def test_recusa_seguida_de_interesse_caso_renato():
    """Cada mensagem tem a sua classe; quem junta as duas é o motor
    (`carrossel_corretor.aplicar_resposta`), que reabre o card só com interesse
    EXPLÍCITO na segunda."""
    primeira = "Já comprei, obrigado"
    segunda = ("Se você tiver uma chácara de até 1000m em condomínio e aceitar "
               "a minha na permuta talvez interessa")
    assert classificar(primeira, "text", 120.0) == rr.RECUSA
    assert classificar(segunda, "text", 400.0) == rr.INTERESSE
    assert rr.interesse_explicito(segunda)


@pytest.mark.parametrize("educacao", [
    "Ok", "Tá bom", "Claro.", "Pode deixar !", "Perfeito",
    "Ok obrigado Deus abençoe", "Claro pode deixar! Agradeço o contato",
    "positivo, muito obrigado e boas vendas",
])
def test_educacao_depois_da_recusa_nao_reabre(educacao):
    """Falas REAIS de quem tinha acabado de dizer não e estava respondendo ao
    pedido de indicação. Viram "interesse" pela afirmativa curta — e por isso
    a reabertura não usa a classe, usa `interesse_explicito`."""
    assert not rr.interesse_explicito(educacao)


@pytest.mark.parametrize("fala", [
    "Se aparecer eu tenho interesse, é que eu tenho que dar minha casa como permuta",
    "Eu quero saber oque você tem",
    "Pergunto e apartamento ou casa no qual bairro",
    "Olá estou vendendo meu apto",
])
def test_interesse_explicito_reabre(fala):
    assert rr.interesse_explicito(fala)


def test_recusa_nunca_e_interesse_explicito():
    assert not rr.interesse_explicito("Não tenho interesse")
    assert not rr.interesse_explicito("Já comprei uma casa no jardim Europa")


# ── "Não quero": o exemplo da Dona ──────────────────────────────────────────

@pytest.mark.parametrize("frase", [
    "Não quero", "nao quero", "Não quero, obrigado", "Oi, não quero não",
    "Não quero saber", "Boa tarde não quero no momento",
    # com imóvel na frase, mas dizendo que parou
    "Boa tarde, eu não quero mais apartamento nenhum, obrigada",
    "Não estou mais procurando casa",
])
def test_nao_quero_e_recusa(frase):
    """O detector de interesse casa a palavra "quero" — sem esta trava, a
    frase que a Dona deu como exemplo de recusa virava card de corretora."""
    assert classificar(frase, "text", 120.0) == rr.RECUSA
    assert not rr.interesse_explicito(frase)


def test_nao_quero_mais_em_mensagem_curta_ja_era_pedido_de_parar():
    """Regra antiga do opt-out, mantida: "não quero mais" / "não quero nada" em
    até 4 palavras é pensamento completo — pare. Vai pra Venda perdida igual,
    e ainda grava o opt-out."""
    assert classificar("não quero mais", "text", 120.0) == rr.PARAR
    assert classificar("não quero mais apartamento", "text", 120.0) == rr.PARAR


@pytest.mark.parametrize("frase", [
    "não quero apartamento, quero casa",
    "Não quero no centro, quero no jardim Europa",
])
def test_preferencia_nao_e_recusa(frase):
    """Dizer o que NÃO quer e, junto, o que quer é lead escolhendo."""
    assert classificar(frase, "text", 120.0) == rr.INTERESSE


def test_nao_quero_mas_quero_vender_e_captacao():
    assert classificar(
        "Não quero comprar, quero vender meu apartamento", "text", 120.0
    ) == rr.INTERESSE


# ── A ordem ────────────────────────────────────────────────────────────────

def test_parar_ganha_do_relogio():
    """Pedido de saída em 2 s é honrado: errar pra esse lado não custa nada;
    ignorar um "pare" custa a qualidade do número."""
    assert classificar("PARAR", "text", 2.0) == rr.PARAR
    assert classificar("Parar mensagens", "button", 1.0) == rr.PARAR


def test_botao_ganha_do_relogio():
    assert classificar("Saiba mais", "button", 1.5) == rr.BOTAO


def test_sem_os_segundos_o_relogio_nao_opina():
    assert classificar("Quero sim", "text", None) == rr.INTERESSE
    assert classificar("Bom dia", None, None) == rr.INDEFINIDA


def test_corte_e_parametro():
    assert classificar("Bom dia", "text", 10.5) == rr.INDEFINIDA
    assert classificar("Bom dia", "text", 10.5, corte_segundos=12) == rr.AUTOMATICA
    # zero desliga o relógio; o conteúdo continua valendo
    assert classificar("Bom dia", "text", 1.0, corte_segundos=0) == rr.INDEFINIDA
    assert classificar(
        "Loja Exemplo agradece seu contato. Como podemos ajudar?", "text", 1.0,
        corte_segundos=0,
    ) == rr.AUTOMATICA


def test_no_limite_do_corte():
    assert classificar("Bom dia", "text", 4.9) == rr.AUTOMATICA
    assert classificar("Bom dia", "text", 5.0) == rr.INDEFINIDA


def test_midia_com_legenda_e_lida_como_texto():
    assert classificar("quero ver esse apartamento", "image", 300.0) == rr.INTERESSE


def test_toda_classe_tem_destino():
    destinos = set(rr.CLASSES_RESPONDEU) | set(rr.CLASSES_VENDA_PERDIDA) | {rr.AUTOMATICA}
    assert destinos == set(rr.CLASSES)
    assert set(rr.CLASSES_QUE_CONTAM) == {rr.INTERESSE, rr.BOTAO}


# ── O detector de saudação automática (ampliado em 05/10) ───────────────────

@pytest.mark.parametrize("texto", [
    "Loja Exemplo agradece seu contato. Como podemos ajudar?",
    "Fulana agradece o contato",
    "Seja bem vindo a Exemplo Tintas🤩 deixe sua mensagem",
    "Olá! Seja muito bem-vindo(a) ao consultório Exemplo!",
    "Bem-vindo(a) à Exemplo Assessoria de Vistos ✈️ Qual serviço você procura ?",
    "SEJA BEM VINDO (A)   !!! No momento não podemos atende-los",
])
def test_saudacao_de_empresa_e_auto_reply(texto):
    assert _e_auto_reply_conteudo(texto)


@pytest.mark.parametrize("texto", [
    "Bom dia", "Quero sim", "Agradeço o contato, não estou",
    "Oi boa tarde, mensagem errada", "Obrigada pelo seu contato... Qual seu nome?",
    "Olá boa tarde tudo bem obrigada por perguntar",
    "Vocês são bem atenciosos, obrigada",
])
def test_gente_nao_vira_auto_reply(texto):
    assert not _e_auto_reply_conteudo(texto)
