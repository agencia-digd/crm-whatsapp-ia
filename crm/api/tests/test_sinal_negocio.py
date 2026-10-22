"""Detector de negociação avançada — as travas que impedem alarme falso.

Nasceu de 17/08: a Lucilene compareceu, entregou documentação e ficou aguardando
crédito. No sistema era no-show + card em Follow-up + lead em remarketing. A
informação estava na CONVERSA e nenhum alerta olhava ali.

Estes testes travam os quatro filtros que fizeram o detector passar de 25
achados com falso positivo pra 6 negociações reais.
"""
from __future__ import annotations

from app.services import sinal_negocio as sn


def test_sinal_forte_e_vocabulario_de_quem_fecha():
    for frase in [
        "entreguei a documentação ontem",
        "meu crédito foi aprovado",
        "vamos assinar o contrato semana que vem",
        "essa é a do inventário de cartório",
        "paguei o sinal",
        "quero fechar",
    ]:
        assert sn._FORTE.search(frase), f"deveria ser forte: {frase}"


def test_interesse_simples_nao_e_negociacao():
    """"Quero ver" e "quanto custa" já são tratados em outro lugar. Aqui é só
    quem está FECHANDO — senão o aviso vira a lista de leads inteira."""
    for frase in ["quero ver um apartamento", "quanto custa?", "pra morar", "tem 2 quartos?"]:
        assert not sn._FORTE.search(frase), f"não deveria ser forte: {frase}"


def test_precisa_de_contexto_imobiliario():
    """A trava mais eficaz — requisito POSITIVO em vez de lista de proibições.

    Em 17/08 apareceram como negociação conversas da Pessoa12 sobre transferência de
    linkpay e liberação de assinatura eletrônica: tinham "documentação" e
    "aprovação", mas nada de imóvel. Perseguir ruído com blocklist é jogo sem
    fim; exigir que a conversa fale de IMÓVEL resolve de uma vez."""
    admin = "estava tentando fazer a transferencia do linkpay mas não tenho o código"
    assert not sn._IMOVEL.search(admin)
    compra = "já vou subir a documentação, a unidade só é reservada pós aprovação"
    assert sn._IMOVEL.search(compra)


def test_ruido_nao_derruba_sinal_forte():
    """O Junyor Neres estava subindo documentação e esperando aprovação pra
    reservar a unidade — e caiu porque a palavra "CNPJ" aparecia na conversa.

    Compra de imóvel MENCIONA CNPJ (empresa do comprador, MEI, imobiliária no
    meio). Ruído só decide quando a evidência é fraca."""
    import inspect
    fonte = inspect.getsource(sn.procurar)
    assert "if not forte and _RUIDO.search(falas)" in fonte, \
        "ruído tem que derrubar só o sinal médio"


def test_medio_precisa_de_dois():
    """Um sinal médio sozinho ("banco", "análise") aparece em qualquer conversa
    de imóvel. Exigir dois é o que separa andamento de conversa fiada."""
    import inspect
    fonte = inspect.getsource(sn.procurar)
    assert "medios < 2" in fonte


def test_etapa_ja_reconhecida_nao_avisa():
    """Quem já está em Proposta/Venda ganha não precisa de aviso — o aviso existe
    pra quem o CRM NÃO está mostrando."""
    assert "Proposta" in sn.ETAPAS_OK
    assert "Venda ganha" in sn.ETAPAS_OK


def test_equipe_da_casa_fica_fora():
    """A Pessoa10 e a Pessoa1 apareciam como negociação todo dia: conversam entre
    si pelas linhas de trabalho, e conversa de corretora é cheia de
    "documentação", "aprovação", "cartório".

    E a causa de fundo: as duas têm SEGUNDO APARELHO, que não estava em
    `internos_acme` — a tabela só aceitava papel='dona' até a migration 0075."""
    import inspect
    fonte = inspect.getsource(sn.procurar)
    assert "_e_da_casa" in fonte
