"""Teste A/B de copy do carrossel — variantes, bucketing e placar.

O que protege:
1. A divisão bate com os pesos pedidos (senão o teste mede outra coisa).
2. O balde do lead é ESTÁVEL — entre restarts e entre rodadas.
3. O relatório conta a categoria QUE SAIU, não a que se pretendia.
4. Marketing não vaza pro caminho da Pessoa12 (regressão real, pega antes do deploy).
"""
from __future__ import annotations

import pytest

from app.services import carrossel_copies as cc


# ── 1. Configuração ─────────────────────────────────────────────────────────

def test_pesos_batem():
    assert cc.conferir_pesos() == []


def test_tem_uma_variante_marketing():
    """Dona 30/07: uma das variantes sai em MARKETING pra comparar categoria."""
    assert any(v.marketing for v in cc.ativas())


def test_maioria_do_volume_e_utility():
    """MARKETING custa ~10x. A cota é teto, não meta."""
    mkt = sum(v.peso for v in cc.ativas() if v.marketing)
    assert mkt <= 30, f"{mkt}% em marketing é muito pra um teste"


# ── 2. Bucketing ────────────────────────────────────────────────────────────

def test_divisao_bate_com_os_pesos():
    """10 mil leads: cada variante fica perto do peso configurado."""
    from collections import Counter

    c = Counter(cc.escolher(i).id for i in range(10_000))
    for v in cc.ativas():
        real = 100 * c[v.id] / 10_000
        assert abs(real - v.peso) < 2, f"{v.id}: pedi {v.peso}%, deu {real:.1f}%"


def test_mesmo_lead_mesma_variante():
    """Estável entre chamadas — e, por usar md5, entre restarts do serviço.
    `hash()` do Python é aleatorizado por processo: o lead trocaria de variante
    a cada deploy e o teste não mediria nada."""
    for lead in (1, 42, 99999, "abc"):
        assert len({cc.escolher(lead).id for _ in range(50)}) == 1


def test_nao_usa_hash_builtin():
    import inspect

    # Só as linhas de CÓDIGO — o comentário explica justamente por que não usar
    # `hash()`, e casar com ele reprovaria o código correto.
    codigo = "\n".join(
        linha.split("#")[0]
        for linha in inspect.getsource(cc.escolher).splitlines()
    )
    assert "md5" in codigo
    assert "hash(" not in codigo.replace("hashlib.", "")


def test_sem_variante_ativa_levanta(monkeypatch):
    """Melhor estourar do que mandar texto vazio pra 5 mil pessoas."""
    monkeypatch.setattr(cc, "VARIANTES", ())
    with pytest.raises(RuntimeError):
        cc.escolher(1)


def test_variantes_mudam_uma_coisa_por_vez():
    """Todas se apresentam como Acme — se a identificação variasse junto com
    a copy, não daria pra saber o que mexeu o ponteiro."""
    for v in cc.ativas():
        assert "Acme" in v.texto


def test_variacao_inspira_na_direta_e_sai_utility():
    """Dona 06/09: a outra copy é variação da campeã, não um texto novo.
    E direta/variação NÃO são o braço de marketing."""
    direta = next(v for v in cc.VARIANTES if v.id == "direta")
    curta = next(v for v in cc.VARIANTES if v.id == "curta")
    assert direta.marketing is False
    assert curta.marketing is False
    assert "ainda está procurando" in curta.texto
    assert "Quer que eu te mande?" in curta.texto
    assert "lançamento" in curta.texto.lower()
    assert curta.texto != direta.texto


def test_texto_cabe_no_parametro_do_template():
    """Meta limita o parâmetro; texto gigante é rejeitado no envio."""
    for v in cc.ativas():
        assert len(v.texto) < 900, f"{v.id} tem {len(v.texto)} chars"


# ── 3. O que é gravado é o que SAIU ─────────────────────────────────────────

def test_grava_categoria_real_nao_a_intencao():
    """Variante marcada marketing pode cair em UTILITY (cota estourada ou
    nenhum template MARKETING aprovado). Gravar a intenção faria o placar
    comparar categoria que nunca foi usada."""
    import inspect

    from app.services import carrossel_corretor as cb

    fonte = inspect.getsource(cb._disparar)
    assert 'r.get("categoria")' in fonte


def test_resposta_herda_a_variante_do_disparo():
    import inspect

    from app.services import carrossel_corretor as cb

    fonte = inspect.getsource(cb._marcar_resposta_na_variante)
    assert "'variante', d.payload->>'variante'" in fonte
    # (05/10/2026) o "último disparo do telefone" virou um SQL com nome, usado
    # também por quem reclassifica a resposta e por quem registra a automática.
    assert "_SQL_ULTIMO_DISPARO_DO_TELEFONE" in fonte
    assert "ORDER BY le.lead_id, le.at DESC" in cb._SQL_ULTIMO_DISPARO_DO_TELEFONE, \
        "tem que ser o ÚLTIMO disparo"


def test_resposta_nao_conta_em_dobro():
    """Lead que manda 3 mensagens seguidas é UM respondente."""
    import inspect

    from app.services import carrossel_corretor as cb

    assert "NOT EXISTS" in inspect.getsource(cb._marcar_resposta_na_variante)


# ── 4. Regressão: marketing não pode vazar pro caminho da Pessoa12 ────────────────

def test_corretor_nao_recebe_kwargs_de_marketing():
    """`enviar_template_corretor` não aceita `permitir_marketing` — passar lá estoura
    TypeError e cala a cadência INTEIRA. Pego antes do deploy; fica travado."""
    import inspect

    from app.api.cloud_corretora_a import enviar_template_corretor
    from app.services.cadencia import _default_sender

    params = inspect.signature(enviar_template_corretor).parameters
    assert "permitir_marketing" not in params, "mudou a assinatura — revisar o guard"

    fonte = inspect.getsource(_default_sender)
    i_corretor = fonte.index("enviar_template_corretor(telefone")
    i_mkt = fonte.index('kwargs["permitir_marketing"]')
    assert i_corretor < i_mkt, "os kwargs de marketing entram DEPOIS do return da Pessoa12"


def test_sender_antigo_nao_quebra():
    """Sender com assinatura curta (fakes, painel) segue funcionando."""
    from app.services.cadencia import _chamar_sender

    def sender_curto(tel, texto, *, com_botao):
        return {"ok": True}

    assert _chamar_sender(sender_curto, "5515", "oi", com_botao=False,
                          permitir_marketing=True, cap_diario_da_linha=50)["ok"]


def test_sender_novo_recebe_os_kwargs():
    from app.services.cadencia import _chamar_sender

    visto = {}

    def sender_completo(tel, texto, *, com_botao, **kw):
        visto.update(kw)
        return {"ok": True}

    _chamar_sender(sender_completo, "5515", "oi", com_botao=False,
                   permitir_marketing=True, cap_diario_da_linha=50)
    assert visto["permitir_marketing"] is True
    assert visto["cap_diario_da_linha"] == 50


# ── 5. O braço de marketing precisa REALMENTE sair em marketing ─────────────

def test_ordem_de_categoria_roda_depois_da_rotacao():
    """A rotação da linha própria é montada DEPOIS do filtro de categoria.
    Ordenar antes não tem efeito — foi o bug: `preferir_marketing` existia e
    não fazia nada pra linha da Corretor, e o braço do teste sairia 100% UTILITY."""
    import inspect

    from app.api.cloud_corretora_a import enviar_template_linha

    fonte = inspect.getsource(enviar_template_linha)
    assert fonte.index("proprios[(ini + k)") < fonte.index("tentativas.sort("), \
        "o sort de categoria tem que vir DEPOIS de montar a rotação própria"


def test_sem_pedir_marketing_ele_fica_por_ultimo():
    mkt = {"m1"}
    t = [("u1",), ("m1",), ("u2",)]
    t.sort(key=lambda x: (x[0] in mkt) is not False)
    assert [x[0] for x in t][-1] == "m1", "cota é teto, não meta — UTILITY primeiro"


def test_pedindo_marketing_ele_vem_primeiro():
    mkt = {"m1"}
    t = [("u1",), ("m1",), ("u2",)]
    t.sort(key=lambda x: (x[0] in mkt) is not True)
    assert [x[0] for x in t][0] == "m1"


# ── Cota de marketing (regra da Dona, relembrada em 04/08/2026) ──────────────


def test_marketing_e_exatamente_a_cota():
    """A cota não é sobre performance — é sobre MIX de categoria. Em 04/08 a
    variante marketing era a segunda pior do teste (6,8%), e aposentá-la por
    isso quebraria a regra. 06/09 a Dona baixou 20→10 no remarketing da Corretor:
    se a soma sair de COTA_MARKETING_PCT, tem que ser decisão explícita, não
    efeito colateral de otimizar taxa de resposta."""
    from app.services.carrossel_copies import COTA_MARKETING_PCT, VARIANTES

    ativas = [v for v in VARIANTES if v.ativa]
    assert sum(v.peso for v in ativas if v.marketing) == COTA_MARKETING_PCT
    assert COTA_MARKETING_PCT == 10


def test_pesos_das_ativas_somam_100():
    from app.services.carrossel_copies import VARIANTES

    assert sum(v.peso for v in VARIANTES if v.ativa) == 100


def test_variante_aposentada_fica_registrada():
    """Perdedora não se apaga: o resultado dela é o que justifica a que entrou
    no lugar."""
    from app.services.carrossel_copies import VARIANTES

    ids = {v.id for v in VARIANTES}
    assert "pergunta" in ids
    assert "brendon_triagem" in ids
    assert not next(v for v in VARIANTES if v.id == "pergunta").ativa
    assert not next(v for v in VARIANTES if v.id == "brendon_triagem").ativa
    assert next(v for v in VARIANTES if v.id == "curta").ativa


def test_teste_de_categoria_usa_o_mesmo_texto():
    """Decisão da Dona (04/08): a variante de marketing tem que usar o MESMO
    texto da utility. Se o texto mudar junto com a categoria, o resultado não
    diz se perdeu por ser marketing ou por ser copy pior — foi o que aconteceu
    com a `marketing_novidade`, que fechou 6,8% sem responder nada."""
    from app.services.carrossel_copies import TEXTO_DIRETA, VARIANTES

    ativas = [v for v in VARIANTES if v.ativa]
    mkt = [v for v in ativas if v.marketing]
    assert mkt, "sumiu a variante de marketing — a cota é regra da casa"
    for v in mkt:
        assert v.texto == TEXTO_DIRETA, (
            f"{v.id} tem texto próprio: o teste de categoria vira teste de copy"
        )
