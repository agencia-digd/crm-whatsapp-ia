"""Matriz de objeções no cérebro de atendimento.

A matriz veio do acervo do Brendon (Liberty), método rodado em 200+
imobiliárias, liberado pra treinar nossa IA em 04/08/2026.

O risco que estes testes seguram não é "sumiu do prompt" — é a matriz
ATROPELAR o método da casa. A fonte é de SDR: reconhece objeção e empurra pro
tour de 10 minutos com dois horários. O método da Acme faz o oposto (pergunta
antes de propor). Se a matriz entrar no mesmo nível, a Assistente vira SDR
agressiva — que não é o tom da casa e não funciona no WhatsApp da corretora.
"""
from __future__ import annotations

from app.persona.objecoes_imobiliarias import MATRIZ_OBJECOES, matriz_objecoes
from app.services.atendimento_brain import brain


def test_matriz_entra_no_cerebro():
    assert MATRIZ_OBJECOES in brain()
    assert MATRIZ_OBJECOES in brain(sugerir=False), (
        "coaching e análise (sugerir=False) também precisam reconhecer objeção"
    )


def test_metodo_vem_antes_da_matriz():
    """A hierarquia é o que impede a Assistente de virar SDR: primeiro entender,
    depois tratar a objeção."""
    b = brain()
    assert b.index("MÉTODO DE ATENDIMENTO") < b.index("OBJEÇÕES")


def test_cobre_as_objecoes_que_mais_aparecem():
    """As que a corretora ouve toda semana. Se alguma sumir num refactor, a
    Assistente volta a improvisar justamente onde o lead trava."""
    for gatilho in (
        "Me passa o valor primeiro",
        "Já tenho corretor",
        "Não tenho entrada",
        "Achei caro pra região",
        "Quero esperar mais um pouco",
        "manda por whatsapp",
    ):
        assert gatilho.lower() in MATRIZ_OBJECOES.lower(), f"faltou: {gatilho}"


def test_nao_ensina_urgencia_fabricada():
    """Trava da casa: nada de escassez inventada pra apressar decisão. A fonte
    original usava ('condições mudam de uma semana pra outra'); foi removido na
    adaptação, e não pode voltar por descuido."""
    proibidos = ["só hoje", "última unidade", "últimas unidades", "por tempo limitado"]
    corpo = MATRIZ_OBJECOES.lower()
    for termo in proibidos:
        assert termo not in corpo or "nunca invente" in corpo, f"apareceu: {termo}"
    assert "nunca invente urgência" in corpo, (
        "a proibição explícita tem que estar escrita — é o que o modelo lê"
    )


def test_manda_perguntar_antes_de_defender_preco():
    """A objeção de preço é onde o SDR e o método mais divergem. Aqui tem que
    ficar claro: descobrir com o que ele compara ANTES de justificar valor."""
    assert "comparado com o que você viu" in MATRIZ_OBJECOES


def test_funcao_de_acesso_devolve_o_texto():
    assert matriz_objecoes() == MATRIZ_OBJECOES
