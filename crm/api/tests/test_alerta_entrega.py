"""O aviso de "a Meta parou de entregar por causa da CONTA".

O caso que motivou (21/09/2026): três pós-ligação da Pessoa7 saíram com HTTP 200
e voltaram `failed` com "Business eligibility payment issue". O painel mostrou
"enviado", ninguém soube, e as três pessoas que tinham acabado de atender a
ligação ficaram sem mensagem nenhuma.

O que os testes travam:
  • falha de CONTA alerta; falha de DESTINATÁRIO não (senão o alerta vira ruído
    diário e a gente aprende a ignorar);
  • um alerta por linha por dia — conta travada derruba o lote inteiro;
  • Telegram fora do ar não marca como avisado (senão o primeiro erro engole o
    único aviso que importava);
  • o webhook de status continua gravando mesmo se o alerta explodir.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.services import alerta_entrega


@pytest.fixture(autouse=True)
def _sem_memoria_de_avisos():
    alerta_entrega._avisados.clear()
    yield
    alerta_entrega._avisados.clear()


@pytest.fixture()
def telegrama(monkeypatch):
    """Captura o que teria ido pro Telegram da Dona."""
    enviados: list[str] = []

    class _Fake:
        def send_to_dona(self, message: str) -> str:
            enviados.append(message)
            return "ok"

    monkeypatch.setattr("app.tools.telegram.TelegramTool", _Fake)
    return enviados


@pytest.mark.parametrize(
    "erro",
    [
        "Business eligibility payment issue",
        "There is an error with the payment method.",
        "business account is not eligible",
    ],
)
def test_falha_de_conta_e_reconhecida(erro):
    assert alerta_entrega.e_falha_de_conta(erro)


@pytest.mark.parametrize("erro", [None, "", "Message undeliverable",
                                  "Re-engagement message"])
def test_falha_do_destinatario_nao_e_de_conta(erro):
    """Número morto é rotina. Alertar nisso é ensinar a ignorar o alerta."""
    assert not alerta_entrega.e_falha_de_conta(erro)


def test_avisa_uma_vez_por_linha_por_dia(telegrama):
    erro = "Business eligibility payment issue"
    assert alerta_entrega.avisar_falha_de_conta(linha="pessoa_d", erro=erro) is True
    assert alerta_entrega.avisar_falha_de_conta(linha="pessoa_d", erro=erro) is False
    assert len(telegrama) == 1, "lote de 80 mensagens não pode virar 80 alertas"

    # Outra linha é outro problema — e outro dia também.
    assert alerta_entrega.avisar_falha_de_conta(linha="corretor", erro=erro) is True
    assert alerta_entrega.avisar_falha_de_conta(
        linha="pessoa_d", erro=erro, hoje=date(2030, 1, 1)
    ) is True
    assert len(telegrama) == 3


def test_o_aviso_diz_a_linha_e_o_motivo(telegrama):
    alerta_entrega.avisar_falha_de_conta(
        linha="pessoa_d", erro="Business eligibility payment issue"
    )
    texto = telegrama[0]
    assert "pessoa_d" in texto
    assert "payment" in texto.lower()
    # Sem o "o que fazer", o alerta é só susto.
    assert "Business Manager" in texto


def test_telegram_fora_do_ar_nao_marca_como_avisado(monkeypatch, telegrama):
    class _Quebrado:
        def send_to_dona(self, message: str) -> str:
            raise RuntimeError("telegram caiu")

    monkeypatch.setattr("app.tools.telegram.TelegramTool", _Quebrado)
    erro = "Business eligibility payment issue"
    assert alerta_entrega.avisar_falha_de_conta(linha="pessoa_d", erro=erro) is False

    # Voltou o Telegram: o aviso ainda tem que sair.
    monkeypatch.undo()
    monkeypatch.setattr("app.tools.telegram.TelegramTool",
                        type("_Ok", (), {"send_to_dona":
                                         lambda self, m: telegrama.append(m) or "ok"}))
    assert alerta_entrega.avisar_falha_de_conta(linha="pessoa_d", erro=erro) is True
