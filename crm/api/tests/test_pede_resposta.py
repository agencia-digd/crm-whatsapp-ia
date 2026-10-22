"""A limpeza do "OK" — `app/services/pede_resposta.py`.

A regra: não é a ÚLTIMA mensagem do lead que importa, é se ela PEDE resposta.
A trava: **na dúvida, MANTÉM na lista.** Erro pra menos custa um clique da
corretora; erro pra mais custa um lead.

Este arquivo é a rede de segurança dessa trava — sem DB, roda sempre.
"""
from __future__ import annotations

import pytest

from app.services.pede_resposta import (
    classificar,
    normalizar,
    pede_resposta,
    refinar_duvidas_com_ia,
)

# ── encerra: NÃO entra na lista ──────────────────────────────────────────────

ENCERRAMENTOS = [
    "ok", "Ok", "OK!", "okay", "okk", "oki",
    "blz", "Blz!", "beleza", "belezaa",
    "valeu", "Valeu!", "vlw", "vlw!!",
    "obrigado", "Obrigada!", "obg", "obgd", "brigado", "obrigadão",
    "muito obrigado", "muito obrigada mesmo",
    "tks", "thanks", "Thanks!",
    "entendi", "Entendido", "entendi!",
    "tá bom", "ta bom", "tá bem", "tá certo", "tá bom então",
    "tudo bem", "tudo certo",
    "de nada", "por nada", "imagina",
    "ok obrigada", "ok, obrigado!", "valeu viu", "ah tá, entendi",
    "era só isso", "só isso",
    "👍", "🙏", "👌", "👍👍", "🙏🏽", "✅", "👍🏻",
]


@pytest.mark.parametrize("texto", ENCERRAMENTOS)
def test_encerramento_sai_da_lista(texto: str) -> None:
    assert pede_resposta(texto) is False, texto


BOLA_COM_O_LEAD = [
    "vou ver e te falo",
    "vou ver com meu marido e te falo",
    "vou pensar e depois te aviso",
    "qualquer coisa eu te chamo",
    "depois eu te falo",
    "vou dar uma olhada e te retorno",
    "assim que eu decidir te aviso",
    "preciso falar com minha esposa",
]


@pytest.mark.parametrize("texto", BOLA_COM_O_LEAD)
def test_bola_com_o_lead_sai_da_lista(texto: str) -> None:
    d = classificar(texto)
    assert d.pede is False, texto
    assert d.motivo == "bola_com_o_lead"


# ── pede resposta: ENTRA na lista ────────────────────────────────────────────

PEDEM = [
    "qual o valor do condomínio?",
    "quanto fica a entrada",
    "me manda o endereço por favor",
    "tem outro parecido nessa região",
    "gostaria de agendar uma visita para sábado de manhã",
    "não entendi a parte do financiamento, pode explicar de novo",
    "ok, mas e a documentação? preciso de mais alguma coisa",
]


@pytest.mark.parametrize("texto", PEDEM)
def test_mensagem_de_verdade_entra_na_lista(texto: str) -> None:
    assert pede_resposta(texto) is True, texto


# ── a TRAVA: os casos de dúvida ficam na lista ───────────────────────────────
#
# Cada um destes é ambíguo de propósito. "sim" pode ser "sim, obrigada" ou
# "sim, quero agendar" — e a segunda hipótese vale um lead. Todos MANTÊM.

DUVIDOSOS = [
    "sim",
    "isso",
    "isso mesmo",
    "certo",
    "perfeito",
    "combinado",
    "fechado",
    "aham",
    "uhum",
    "bom dia",       # abertura de conversa, não despedida
    "boa tarde",
    "oi",
    "oi!!",
    "tenho interesse",
    "pode ser",
    "😡",            # emoji fora da lista de agradecimento
    "🤔",
]


@pytest.mark.parametrize("texto", DUVIDOSOS)
def test_na_duvida_mantem_na_lista(texto: str) -> None:
    d = classificar(texto)
    assert d.pede is True, f"{texto!r} saiu da lista — a trava foi violada"
    assert d.duvida is True, f"{texto!r} deveria estar marcado como dúvida"


def test_pergunta_vence_qualquer_encerramento() -> None:
    # "ok?" é pergunta, não "ok"
    assert pede_resposta("ok?") is True
    assert classificar("obrigado, mas e o IPTU?").motivo == "pergunta"


def test_midia_sempre_pede_resposta() -> None:
    # não dá pra ler áudio/foto aqui — ignorar um áudio é perder um lead
    for tipo in ("audio", "image", "video", "document", "sticker", "ptt"):
        d = classificar(None, tipo)
        assert d.pede is True
        assert d.motivo == "midia"
    # áudio com legenda vazia idem
    assert classificar("", "audio").pede is True


def test_texto_vazio_mantem() -> None:
    assert classificar(None).motivo == "sem_texto"
    assert classificar("   ").pede is True


def test_mensagem_longa_nunca_e_encerramento() -> None:
    longa = "ok " * 20
    assert pede_resposta(longa) is True


def test_normalizar() -> None:
    assert normalizar("Tá Bom!!") == "ta bom"
    assert normalizar("OBRIGADA 🙏") == "obrigada"


# ── camada 2 (IA) — só na dúvida, e só pode TIRAR da lista ───────────────────


class _ProviderFake:
    def __init__(self, resposta: str) -> None:
        self.resposta = resposta
        self.chamadas = 0

    def complete(self, system: str, user: str, json_schema: dict | None = None):
        self.chamadas += 1
        return self.resposta, None


def test_ia_devolve_mapa_por_indice() -> None:
    p = _ProviderFake('{"itens":[{"i":0,"pede":false},{"i":1,"pede":true}]}')
    out = refinar_duvidas_com_ia(["ok então", "pode ser"], provider=p)
    assert out == {0: False, 1: True}
    assert p.chamadas == 1


def test_ia_falhou_devolve_vazio_e_mantem_tudo() -> None:
    class _Explode:
        def complete(self, system: str, user: str, json_schema: dict | None = None):
            raise RuntimeError("modelo fora")

    assert refinar_duvidas_com_ia(["sim"], provider=_Explode()) == {}


def test_ia_json_torto_nao_derruba() -> None:
    assert refinar_duvidas_com_ia(["sim"], provider=_ProviderFake("não é json")) == {}
    # item sem índice válido some do mapa → chamador mantém na lista
    p = _ProviderFake('{"itens":[{"pede":false},{"i":99,"pede":false}]}')
    assert refinar_duvidas_com_ia(["sim"], provider=p) == {}


def test_ia_respeita_o_limite() -> None:
    p = _ProviderFake('{"itens":[]}')
    refinar_duvidas_com_ia([f"msg {i}" for i in range(50)], provider=p, limite=5)
    assert p.chamadas == 1


def test_ia_sem_candidatos_nao_chama_modelo() -> None:
    p = _ProviderFake('{"itens":[]}')
    assert refinar_duvidas_com_ia([], provider=p) == {}
    assert p.chamadas == 0
