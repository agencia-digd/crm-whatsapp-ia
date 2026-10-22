

# ── FORMA: o que a mensagem FAZ (05/08) ──────────────────────────────────────
#
# A similaridade textual media "escreveu igual?" e dava 0% — duas mensagens
# certas raramente têm as mesmas palavras. A forma mede "faria a mesma coisa?",
# que é o que decide se a IA está pronta.

from app.services.comparador_sombra import FORMAS, forma  # noqa: E402


def test_forma_pergunta_ganha_de_tudo():
    """Se pediu algo, a bola está com o lead — mesmo que a mensagem prometa."""
    assert forma("Qual dia fica melhor pra você?") == "pergunta"
    assert forma("Te mando as opções hoje. Qual bairro te interessa mais?") == "pergunta"


def test_forma_promessa_ganha_de_despedida():
    """"Te mando amanhã, qualquer coisa me chama" é compromisso com rodapé, não
    despedida. Classificar como despedida contaria errado o que importa."""
    assert forma("Te mando as 3 opções amanhã cedo, qualquer coisa me chama 😊") == "promessa"
    assert forma("Vou confirmar com o proprietário e te falo ainda hoje.") == "promessa"


def test_forma_despedida_e_o_que_encerra():
    assert forma("Fico à disposição!") == "despedida"
    assert forma("Boa sorte na busca! 😊") == "despedida"


def test_forma_afirmacao_quando_nao_pede_nem_promete():
    assert forma("O condomínio desse é 380 por mês.") == "afirmacao"


def test_forma_sem_texto():
    assert forma(None) is None
    assert forma("   ") is None
    assert forma("😊") is None      # só emoji não é mensagem comparável


def test_formas_conhecidas():
    assert set(FORMAS) == {"pergunta", "promessa", "despedida", "afirmacao"}


def test_placar_traz_forma_e_despedidas():
    """O placar tem que responder duas perguntas novas: a IA faria a mesma coisa
    que a corretora? E quanto ela ainda encerra conversa viva?"""
    from app.services.comparador_sombra import agregar_placar

    pares = [
        {"veredito": "nao_bateu", "similaridade": 0.1,
         "rascunho": "Qual dia fica melhor?", "resposta_real": "Que horas te atende?"},
        {"veredito": "nao_bateu", "similaridade": 0.1,
         "rascunho": "Fico à disposição!", "resposta_real": "Consigo terça, pode ser?"},
    ]
    p = agregar_placar(pares)
    assert p["mesma_forma_pct"] == 50          # uma bateu na forma, a outra não
    assert p["despedidas_ia_pct"] == 50
    assert p["forma_ia"]["pergunta"] == 1
    assert p["forma_ia"]["despedida"] == 1
