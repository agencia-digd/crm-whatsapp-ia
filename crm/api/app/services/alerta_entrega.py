"""Avisa a Dona quando a Meta recusa entrega por problema de CONTA.

Por que existe (21/09/2026)
---------------------------
A Pessoa7 passou a manhã ligando. Três pessoas atenderam e pediram pra continuar
no WhatsApp; nas três o copiloto montou o template certo, a Graph respondeu 200
com wamid e o painel mostrou "enviado". Segundos depois o webhook trouxe
`failed` — "Business eligibility payment issue", a WABA sem método de pagamento
válido. As três ficaram sem nada e ninguém soube até a Dona estranhar à tarde.

O buraco não é o envio, é o SILÊNCIO: o 200 da Graph diz "aceitei", não
"entreguei", e o `failed` só existia dentro do `conteudo_metadata`. Erro de
conta não se resolve tentando de novo — alguém precisa ir no Business Manager.
Por isso o aviso vai pra Dona, não pra corretora: a corretora não tem o que
fazer com essa informação.

Escopo do alerta
----------------
Só falha de CONTA (pagamento / elegibilidade do negócio). `Message
undeliverable` (número morto, aparelho sem WhatsApp) é rotina e acontece todo
dia — alertar nisso ensina a ignorar o alerta.

Uma vez por linha por dia
-------------------------
Quando a conta trava, trava pra TODAS as mensagens daquela linha: sem o
silenciador, um lote de remarketing viraria 80 alertas idênticos. A marca do
"já avisei" fica em memória do processo — reiniciar o serviço faz o aviso sair
de novo no próximo `failed`, que é o lado certo de errar (repetir um alerta é
barato; engolir o primeiro é o que a gente está consertando).

Nada aqui pode derrubar o webhook: o status de entrega é registro, o alerta é
consequência.
"""
from __future__ import annotations

from datetime import date

import structlog

log = structlog.get_logger()

# Trechos que identificam falha de CONTA no `delivery_error` da Meta. Comparados
# em minúsculas, por substring — a Meta muda a redação sem avisar, mas mantém as
# palavras.
_MARCAS_DE_CONTA = ("payment", "eligibility", "business account")

# (slug da linha, dia) já avisado. Ver "Uma vez por linha por dia".
_avisados: set[tuple[str, date]] = set()


def e_falha_de_conta(erro: str | None) -> bool:
    """True quando o motivo da recusa é a conta, não o destinatário."""
    e = (erro or "").lower()
    return any(m in e for m in _MARCAS_DE_CONTA)


def avisar_falha_de_conta(*, linha: str, erro: str | None, hoje: date | None = None) -> bool:
    """Manda o alerta pra Dona. Devolve True se o aviso saiu agora.

    Args:
        linha: slug ou instance da linha (o que a Dona lê pra saber de quem é).
        erro: `delivery_error` como veio da Meta.
    """
    if not e_falha_de_conta(erro):
        return False

    chave = (str(linha), hoje or date.today())
    if chave in _avisados:
        return False
    _avisados.add(chave)

    texto = (
        f"⚠️ *Linha {linha}*: a Meta está RECUSANDO as mensagens.\n\n"
        f"Motivo: _{erro}_\n\n"
        "É a conta, não o número — o envio sai com 200 e o lead não recebe "
        "nada. Conferir o método de pagamento da WABA no Business Manager.\n\n"
        "Quando voltar: `python scripts/reenviar_falhas_pagamento.py --aplicar`"
    )

    try:
        from app.tools.telegram import TelegramTool

        TelegramTool().send_to_dona(texto)
        log.warning("alerta_falha_de_conta_enviado", linha=linha, erro=(erro or "")[:80])
        return True
    except Exception as e:  # noqa: BLE001 — alerta nunca derruba o webhook
        # Solta a marca: se o Telegram falhou, ninguém foi avisado, e o próximo
        # `failed` deve tentar de novo em vez de achar que já avisou.
        _avisados.discard(chave)
        log.error("alerta_falha_de_conta_nao_saiu", linha=linha, erro=str(e)[:160])
        return False
