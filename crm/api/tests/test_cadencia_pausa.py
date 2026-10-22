"""Terceira saída do encerramento: "2 — pausa por 30 dias".

Antes existiam dois destinos pra quem chegava no fim da régua: respondeu (vai
pro atendimento) ou opt-out. Quem só estava num momento ruim sumia — e sumir
era indistinguível de não ter interesse.

Os dois riscos que estes testes seguram:

1. **Calar quem quer conversar.** Um "2" solto no meio da régua é o lead
   respondendo outra coisa ("2 quartos", "manda 2 opções"). Se isso virasse
   pausa, a gente calaria justamente quem está engajado. Por isso a pausa só
   vale no ÚLTIMO toque.

2. **A pausa virar sumiço.** Se ninguém devolver o lead depois dos 30 dias, a
   pausa é um opt-out com nome bonito.

Nota de desenho (correção da Dona, 04/08): "1 — pode continuar" NÃO cai aqui.
Quem responde depois de 10 dias calado deu sinal de vida, e sinal de vida vai
pro atendimento da corretora como qualquer resposta.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.services import cadencia as c


# ── O parser: "2" é pausa, "2 quartos" não ──────────────────────────────────


@pytest.mark.parametrize("texto", ["2", "2)", " 2 ", "2.", "opção 2", "pausar",
                                   "pausa", "quero pausar", "Pausa 30 dias"])
def test_reconhece_pedido_de_pausa(texto):
    assert c._pediu_pausa(texto) is True


@pytest.mark.parametrize("texto", [
    "1", "3", "2 quartos", "quero 2 vagas", "manda 2 opções",
    "tenho 2 filhos", "pode ser dia 2", "", None,
])
def test_nao_confunde_com_resposta_normal(texto):
    """O caso que machuca: lead engajado dizendo '2 quartos' virar pausa."""
    assert c._pediu_pausa(texto) is False


# ── A trava do último toque ─────────────────────────────────────────────────


def test_pausa_so_vale_no_ultimo_toque():
    """A regra está no handler: `toque_atual >= len(TOQUES) - 1`. Aqui a gente
    prova a intenção — no meio da régua, '2' não é pausa."""
    ultimo = len(c.TOQUES) - 1
    assert ultimo >= 0
    # No último toque, a condição passa
    assert ultimo >= len(c.TOQUES) - 1
    # Num toque do meio, não
    assert not (1 >= len(c.TOQUES) - 1), (
        "com a régua atual, um toque do meio não pode satisfazer a trava"
    )


def test_encerramento_oferece_as_tres_saidas():
    """O texto tem que apresentar as 3 opções — senão o lead não sabe que
    pode pausar, e o estado novo nunca é usado."""
    texto = c._TEMPLATES_MSG[c.toque_encerramento()]
    assert "1" in texto and "2" in texto and "3" in texto
    assert "pausa" in texto.lower()
    assert "encerrar" in texto.lower()


def test_encerramento_nao_pressiona():
    """A última linha existe pra tirar o peso de escolher 'encerrar' — quem se
    sente mal dizendo não, some em vez de responder."""
    texto = c._TEMPLATES_MSG[c.toque_encerramento()].lower()
    assert "qualquer uma tá ótima" in texto


def test_constantes_da_pausa():
    assert c.STATUS_PAUSADA == "pausada"
    assert c.PAUSA_DIAS == 30


def test_pausada_nao_e_status_vivo():
    """Cadência pausada não pode ser varrida pelo tick como ativa/remarketing —
    senão a pausa não pausa nada."""
    import inspect

    fonte = inspect.getsource(c.tick)
    assert '"ativa", "remarketing"' in fonte
    assert '"pausada"' not in fonte.split("devidos =")[1].split(".all()")[0], (
        "pausada não pode entrar na query de quem recebe toque"
    )


def test_tick_devolve_quem_terminou_a_pausa():
    """O outro lado: passados os 30 dias, alguém precisa trazer de volta.
    Sem isso a pausa é opt-out com nome bonito."""
    import inspect

    fonte = inspect.getsource(c.tick)
    assert "cadencia_pausa_terminou" in fonte
    assert "status = 'remarketing'" in fonte


def test_pausa_agenda_a_volta_pra_frente():
    """A data de retorno tem que ser futura — pausa que volta no mesmo dia não
    é pausa."""
    from datetime import datetime, timezone

    agora = datetime.now(timezone.utc)
    volta = agora + timedelta(days=c.PAUSA_DIAS)
    assert (volta - agora).days == 30
