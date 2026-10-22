"""Pesos do teste A/B do carrossel editáveis pelo painel — e as duas travas.

A tela do remarketing deixa a Dona mexer na FATIA de cada variante. Duas coisas
não podem cair por causa disso:

  1. a soma das ativas tem que dar 100 — o bucketing divide em cima da soma, e
     com 90 a "fatia de 40%" vira 44% sem ninguém ver;
  2. a fatia em MARKETING fica na cota — é mix de categoria (regra da casa), não
     performance. `test_carrossel_copies.test_marketing_e_exatamente_a_cota`
     trava o CÓDIGO; se a validação do painel não travasse também, o teste
     continuaria verde com a operação fora do combinado.

E o texto: `direta` e `direta_marketing` usam o MESMO texto de propósito (é
teste de CATEGORIA). Por isso a copy do carrossel NÃO é editável pela tela —
editar um lado só transforpessoa o teste de categoria em teste de copy.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import automacoes as api
from app.services import carrossel_copies as cc


def _sem_override(monkeypatch):
    """Painel limpo: pesos efetivos = os do código."""
    monkeypatch.setattr(cc, "pesos_efetivos", cc.pesos_padrao)


# ── 1. A validação aceita o que é combinado ──────────────────────────────────


def test_aceita_os_pesos_de_hoje():
    assert cc.validar_pesos(cc.pesos_padrao()) == cc.pesos_padrao()


def test_aceita_remanejar_entre_as_utility():
    """Trocar volume entre variantes UTILITY é a edição do dia a dia."""
    novo = dict(cc.pesos_padrao(), direta=60, curta=30)
    assert cc.validar_pesos(novo)["direta"] == 60


def test_mapa_parcial_completa_com_o_padrao():
    """A tela manda só o que mudou; o que falta vale o peso do código."""
    out = cc.validar_pesos({"direta": 60, "curta": 30})
    assert set(out) == set(cc.pesos_padrao())
    assert out["direta_marketing"] == cc.pesos_padrao()["direta_marketing"]


# ── 2. TRAVA 1: a soma das ativas tem que dar 100 ────────────────────────────


@pytest.mark.parametrize("direta,curta", [(30, 40), (50, 50), (0, 0)])
def test_recusa_soma_diferente_de_100(direta, curta):
    with pytest.raises(ValueError, match="não 100"):
        cc.validar_pesos(
            dict(cc.pesos_padrao(), direta=direta, curta=curta)
        )


# ── 3. TRAVA 2: MARKETING fica em 20% ────────────────────────────────────────


def test_recusa_derrubar_a_cota_de_marketing():
    """Soma 100, mas marketing em 0% — passaria numa validação ingênua."""
    with pytest.raises(ValueError, match="MARKETING"):
        cc.validar_pesos(
            dict(cc.pesos_padrao(), direta=50, curta=50, direta_marketing=0)
        )


def test_recusa_zerar_o_braco_de_marketing():
    with pytest.raises(ValueError, match="MARKETING"):
        cc.validar_pesos(
            dict(cc.pesos_padrao(), direta=50, curta=50, direta_marketing=0)
        )


def test_recusa_estourar_a_cota_de_marketing():
    with pytest.raises(ValueError, match="MARKETING"):
        cc.validar_pesos(
            dict(cc.pesos_padrao(), direta=40, curta=40, direta_marketing=20)
        )


def test_a_cota_bate_com_a_do_codigo():
    """O número da trava é o mesmo que o teste de copy protege."""
    ativas = [v for v in cc.VARIANTES if v.ativa]
    assert sum(v.peso for v in ativas if v.marketing) == cc.COTA_MARKETING_PCT


# ── 4. Entrada malformada ────────────────────────────────────────────────────


def test_recusa_variante_desconhecida():
    with pytest.raises(ValueError, match="desconhecida"):
        cc.validar_pesos(dict(cc.pesos_padrao(), copy_nova=10))


@pytest.mark.parametrize("peso", ["40", 40.5, True, None])
def test_recusa_peso_que_nao_e_inteiro(peso):
    with pytest.raises(ValueError, match="inteiro"):
        cc.validar_pesos({"direta": peso})


@pytest.mark.parametrize("peso", [-1, 101])
def test_recusa_peso_fora_da_faixa(peso):
    with pytest.raises(ValueError, match="faixa"):
        cc.validar_pesos({"direta": peso})


def test_recusa_nao_mapa():
    with pytest.raises(ValueError, match="mapa"):
        cc.validar_pesos([40, 40, 20])  # type: ignore[arg-type]


def test_nao_reativa_variante_aposentada_pela_tela():
    """A `pergunta` e a `brendon_triagem` foram desligadas com resultado
    documentado no código. Voltar sem esse contexto é reabrir um teste que
    já terminou."""
    with pytest.raises(ValueError, match="aposentada"):
        cc.validar_pesos(dict(cc.pesos_padrao(), pergunta=20, direta_marketing=10,
                              direta=30, curta=40))
    with pytest.raises(ValueError, match="aposentada"):
        cc.validar_pesos(dict(cc.pesos_padrao(), brendon_triagem=40, direta=50,
                              curta=0, direta_marketing=10))


# ── 5. A trava vale no ENDPOINT, não só na função ────────────────────────────


def test_endpoint_recusa_furar_a_cota():
    with pytest.raises(HTTPException) as e:
        api._validar_config(
            "carrossel_pesos",
            dict(cc.pesos_padrao(), direta=50, curta=50, direta_marketing=0),
        )
    assert e.value.status_code == 422
    assert "MARKETING" in str(e.value.detail)


def test_endpoint_recusa_soma_errada():
    with pytest.raises(HTTPException) as e:
        api._validar_config("carrossel_pesos", {"direta": 10})
    assert e.value.status_code == 422


def test_endpoint_aceita_o_combinado():
    assert api._validar_config("carrossel_pesos", cc.pesos_padrao()) == cc.pesos_padrao()


def test_endpoint_valida_o_interruptor():
    assert api._validar_config("carrossel_corretor_ativo", True) is True
    with pytest.raises(HTTPException):
        api._validar_config("carrossel_corretor_ativo", "sim")


# ── 6. O override chega no motor ─────────────────────────────────────────────


def test_pesos_efetivos_usam_o_override(monkeypatch):
    from app.services import config_operacional

    monkeypatch.setattr(
        config_operacional, "get",
        lambda chave: {"direta": 60, "curta": 30} if chave == cc.CHAVE_PESOS
        else None,
    )
    assert cc.pesos_efetivos()["direta"] == 60
    assert cc.pesos_efetivos()["curta"] == 30
    # quem não foi mexido segue no valor do código
    assert cc.pesos_efetivos()["direta_marketing"] == 10


def test_sem_override_vale_o_codigo(monkeypatch):
    from app.services import config_operacional

    monkeypatch.setattr(config_operacional, "get", lambda chave: {})
    assert cc.pesos_efetivos() == cc.pesos_padrao()


def test_banco_fora_nao_para_o_disparo(monkeypatch):
    """Override indisponível cai nos pesos do código — o carrossel não fica
    sem variante porque uma tabela de configuração não respondeu."""
    from app.services import config_operacional

    def explode(chave):
        raise RuntimeError("banco fora")

    monkeypatch.setattr(config_operacional, "get", explode)
    assert cc.pesos_efetivos() == cc.pesos_padrao()
    assert cc.escolher(1).id in {v.id for v in cc.VARIANTES}


def test_bucketing_segue_o_peso_editado(monkeypatch):
    """Editar o peso muda a divisão de verdade — senão o controle é decorativo."""
    from collections import Counter

    from app.services import config_operacional

    monkeypatch.setattr(
        config_operacional, "get",
        lambda chave: {"direta": 60, "curta": 30, "direta_marketing": 10}
        if chave == cc.CHAVE_PESOS else None,
    )
    c = Counter(cc.escolher(i).id for i in range(10_000))
    assert abs(100 * c["direta"] / 10_000 - 60) < 2


def test_placar_mostra_o_peso_que_esta_valendo(monkeypatch):
    from app.services import config_operacional

    monkeypatch.setattr(
        config_operacional, "get",
        lambda chave: {"direta": 60} if chave == cc.CHAVE_PESOS else None,
    )
    assert cc.por_id("direta").peso == 60


# ── 7. Copy do carrossel não se edita pela tela ──────────────────────────────


def test_o_mapa_diz_qual_texto_se_edita(monkeypatch):
    """Desde 05/10/2026 o texto da variante ATIVA se edita na tela. Não se
    edita: a aposentada, e a de marketing enquanto espelha a `direta` (é o
    teste de categoria — editar uma só viraria teste de copy)."""
    _sem_override(monkeypatch)
    mapa = api._mapa_carrossel_corretor()
    assert mapa["variantes"], "sumiram as variantes do mapa"
    por_id = {v["id"]: v for v in mapa["variantes"]}
    assert por_id["direta"]["texto_editavel"] is True
    assert por_id["curta"]["texto_editavel"] is True
    assert por_id["direta_marketing"]["texto_editavel"] is False
    assert por_id["direta_marketing"]["espelhando"] is True
    assert por_id["pergunta"]["texto_editavel"] is False


def test_o_mapa_amarra_as_variantes_que_dividem_o_texto(monkeypatch):
    """Quem vê duas caixas com o mesmo texto precisa saber que são a MESMA copy
    — senão edita uma e o teste de categoria vira teste de copy."""
    _sem_override(monkeypatch)
    por_id = {v["id"]: v for v in api._mapa_carrossel_corretor()["variantes"]}
    assert por_id["direta_marketing"]["texto_igual_a"] == ["direta"]
    assert por_id["direta"]["texto_igual_a"] == ["direta_marketing"]
    assert por_id["direta"]["texto"] == por_id["direta_marketing"]["texto"]


def test_o_mapa_leva_a_cota_pra_tela(monkeypatch):
    _sem_override(monkeypatch)
    assert api._mapa_carrossel_corretor()["cota_marketing_pct"] == cc.COTA_MARKETING_PCT
