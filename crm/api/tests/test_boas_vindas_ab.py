"""Teste A/B/C da BOAS-VINDAS — bucketing, pesos e validação da copy.

O que está em jogo: a primeira mensagem que o lead novo recebe passou a ter três
copies concorrendo. Se o balde não for determinístico, o mesmo lead pode cair em
variantes diferentes entre um webhook e outro — e aí o resultado do teste não
significa nada. Se o peso não for validado, dá pra deixar a soma em 70% e metade
dos leads sai sem regra nenhuma.

As garantias que os testes seguram:
  1. o balde é do LEAD e é estável (mesmo id → mesma variante, sempre);
  2. a distribuição respeita os pesos;
  3. peso fora da faixa / soma ≠ 100 / variante desconhecida são recusados;
  4. variante aposentada no código não volta pela tela;
  5. a copy validada recusa placeholder que o motor não substitui e "Saiba mais"
     no corpo (o template já fecha com o botão);
  6. o texto que SAI carrega o nome da corretora da linha.

Nenhum id de variante é fixado: tudo derivado de `VARIANTES`.
"""
from __future__ import annotations

import collections

import pytest

from app.services import boas_vindas_copies as bvc

IDS = [v.id for v in bvc.VARIANTES]
CONTROLE = bvc.CONTROLE_ID


# ── 1. bucketing determinístico ─────────────────────────────────────────────


def test_mesmo_lead_cai_sempre_na_mesma_variante():
    for lead_id in (1, 7, 99, "abc", 123456):
        escolhas = {bvc.escolher(lead_id).id for _ in range(50)}
        assert len(escolhas) == 1, f"lead {lead_id} mudou de variante"


def test_leads_diferentes_se_espalham_pelas_variantes():
    vistas = {bvc.escolher(i).id for i in range(500)}
    assert vistas == set(v.id for v in bvc.ativas())


def test_sem_lead_id_vale_o_controle_nunca_sorteio():
    """Preview de tela não pode mudar a cada F5."""
    assert bvc.escolher(None).id == CONTROLE


def test_distribuicao_segue_os_pesos():
    n = 6000
    conta = collections.Counter(bvc.escolher(i).id for i in range(n))
    for v in bvc.ativas():
        fatia = conta[v.id] / n * 100
        assert abs(fatia - v.peso) < 4, f"{v.id}: {fatia:.1f}% pra peso {v.peso}"


# ── 2. pesos ────────────────────────────────────────────────────────────────


def test_pesos_do_codigo_somam_100():
    assert bvc.conferir_pesos() == []


def test_validar_pesos_devolve_mapa_completo():
    metade = {IDS[0]: 100}
    zerados = dict.fromkeys(IDS[1:], 0)
    out = bvc.validar_pesos({**metade, **zerados})
    assert set(out) == set(IDS)
    assert out[IDS[0]] == 100


@pytest.mark.parametrize(
    "pesos, pedaco",
    [
        ({"variante_que_nao_existe": 50}, "desconhecida"),
        ({IDS[0]: "50"}, "número inteiro"),
        ({IDS[0]: 140}, "fora da faixa"),
        ({IDS[0]: 10}, "somam"),
    ],
)
def test_peso_invalido_e_recusado(pesos, pedaco):
    with pytest.raises(ValueError) as e:
        bvc.validar_pesos(pesos)
    assert pedaco in str(e.value)


def test_variante_fora_da_rodada_nao_entra_pela_tela():
    """Vale pras duas: a que perdeu (reabrir teste encerrado) e a que ainda não
    competiu (entrar na fila sem a decisão registrada no código)."""
    fora = [v for v in bvc.VARIANTES if not v.ativa]
    assert fora, "o teste perde o sentido se todas estiverem na rodada"
    with pytest.raises(ValueError) as e:
        bvc.validar_pesos({fora[0].id: 20})
    assert "fora da rodada" in str(e.value)


def test_aguardando_nao_e_aposentada():
    """Palavras diferentes porque significam coisas diferentes: uma ainda não
    competiu, a outra perdeu. A tela lê este campo."""
    for v in bvc.VARIANTES:
        estado = bvc.estado_da_variante(v)
        if v.ativa and v.peso > 0:
            assert estado == bvc.ESTADO_NA_RODADA
        elif v.aguardando:
            assert estado == bvc.ESTADO_AGUARDANDO
        else:
            assert estado == bvc.ESTADO_APOSENTADA


def test_rodada_1_e_controle_contra_uma_variante():
    """Decisão da Dona (05/08): A × B na rodada 1, C esperando a vez. O
    controle NUNCA sai da fila — sem linha de base, troca-se por trocar."""
    ativas = bvc.ativas()
    assert len(ativas) == 2
    assert any(v.id == bvc.CONTROLE_ID for v in ativas), "o controle saiu da fila"
    assert sum(v.peso for v in ativas) == 100
    assert [v.peso for v in ativas] == [50, 50]


# ── 3. copy ─────────────────────────────────────────────────────────────────


def test_copy_valida_passa():
    assert bvc.validar_texto("  Sou a {nome}, da Acme. Tudo certo?  ").startswith("Sou")


@pytest.mark.parametrize(
    "texto, pedaco",
    [
        ("   ", "vazia"),
        ("x" * (bvc.TEXTO_MAX_CHARS + 1), "longa demais"),
        ("Sou a {nome} e {{1}} vai sair literal", "placeholder"),
        ("Sou a {nome}. Toque em Saiba mais", "Saiba mais"),
        # MALFORMADAS: quem substitui é `str.replace("{nome}", ...)`, então
        # qualquer chave que não seja exatamente {nome} chega literal no
        # WhatsApp do cliente. A regex antiga deixava as três passarem.
        ("Sou a {nome, da Acme", "placeholder"),
        ("Sou a {nome!r}, da Acme", "placeholder"),
        ("Sou a {} da Acme", "placeholder"),
        ("Sou a {nome}} da Acme", "placeholder"),
    ],
)
def test_copy_invalida_e_recusada(texto, pedaco):
    with pytest.raises(ValueError) as e:
        bvc.validar_texto(texto)
    assert pedaco in str(e.value)


def test_validar_copies_apaga_override_quando_vem_vazio():
    assert bvc.validar_copies({IDS[0]: "  "}) == {}


# ── 4. o texto que sai ──────────────────────────────────────────────────────


def test_toda_variante_carrega_o_nome_da_linha():
    for v in bvc.VARIANTES:
        assert bvc.PLACEHOLDER_NOME in v.texto
        assert "Pessoa5" in v.render("Pessoa5")
        assert bvc.PLACEHOLDER_NOME not in v.render("Pessoa5")


def test_variantes_nao_repetem_o_cta_do_template():
    """O template reeng_b já fecha com 'Saiba mais' — repetir vira gagueira."""
    for v in bvc.VARIANTES:
        assert "saiba mais" not in v.texto.lower()
        assert "fico à disposição" not in v.texto.lower()
        assert not v.texto.lstrip().lower().startswith(("oi", "olá", "ola"))


def test_boas_vindas_texto_usa_a_variante_do_lead():
    from app.services.lead_intake import boas_vindas_texto, nome_da_linha

    for lead_id in (1, 2, 3, 4, 5):
        esperado = bvc.escolher(lead_id).render(nome_da_linha(1))
        assert boas_vindas_texto(1, lead_id=lead_id) == esperado
    # sem lead: controle, com o nome da linha pedida
    assert nome_da_linha(3) in boas_vindas_texto(3)
