

def test_template_leva_o_mesmo_resumo_do_texto_livre():
    """Correção da Dona (04/08): template TEM variável, e o resumo cabe dentro
    dela. Antes o fallback mandava só a busca, sem a renda — e como a janela de
    24h da linha da Pessoa12 quase nunca está aberta, o template é o caminho NORMAL.
    Na prática o resumo bom quase nunca saía."""
    from app.agents.pessoa.agent import corpo_abertura_corretor, _mensagem_abertura_corretor

    q = {"regiao": "Bairro X", "quartos": "2", "finalidade": "morar",
         "renda": "R$3.000"}
    corpo = corpo_abertura_corretor(q)
    assert "R$3.000" in corpo, "a renda sumiu do resumo"
    assert "2 quartos" in corpo
    # o que vai no texto livre contém exatamente o mesmo corpo
    assert corpo in _mensagem_abertura_corretor(q, "Corretor")


def test_resumo_aguenta_qualificacao_vazia():
    """Lead que respondeu mas contou pouco: a abertura não pode virar frase
    quebrada nem prometer resumo que não existe."""
    from app.agents.pessoa.agent import corpo_abertura_corretor

    corpo = corpo_abertura_corretor({})
    assert "te resumo" not in corpo
    assert corpo.endswith(".")


def test_abertura_sai_sempre_por_template(monkeypatch):
    """Decisão da Dona (04/08): a abertura na linha da Pessoa12 é template UTILITY,
    com o resumo na variável — não texto livre com template de reserva.

    A janela de 24h é POR NÚMERO: o lead escreveu pro 0000, não pra linha da
    Pessoa12. Tentar texto livre ali era uma chamada que quase sempre falhava, e
    falha de entrega conta contra a qualidade da linha na Meta."""
    from app.agents.pessoa import agent as m
    from app.api import cloud_corretora_a as corretor

    chamadas: dict[str, object] = {}

    def _template(phone, nome, hook=None, cta=None, **kw):
        chamadas["hook"] = hook
        return {"ok": True}

    def _livre(*a, **kw):
        raise AssertionError("não pode tentar texto livre na abertura da Pessoa12")

    monkeypatch.setattr(corretor, "enviar_template_corretor", _template)
    monkeypatch.setattr(corretor, "enviar_cloud", _livre, raising=False)

    q = {"regiao": "Bairro X", "quartos": "2", "renda": "R$3.000"}
    res = m._abrir_conversa_na_corretor("+5500000000000", "Corretor", q)

    assert res["aberta"] is True and res["via"] == "template"
    assert "R$3.000" in str(chamadas["hook"]), "o resumo tem que ir na variável"
