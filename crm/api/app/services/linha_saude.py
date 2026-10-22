"""A linha está entregando, ou a Meta está recusando tudo?

Existe por um estrago repetido em 18/09/2026: as WABAs da Pessoa10 e da Pessoa11
travaram em "Business eligibility payment issue" (forma de pagamento) e o
painel seguiu dizendo "enviado ✔️" pra corretora. A Pessoa10 mandou pra Sonia às
18:46, leu "enviado", e a mensagem nunca chegou — a recusa da Meta só aparece
segundos depois, no webhook de status, quando a tela já deu o OK.

O envio é assíncrono por natureza: a Graph aceita (200) e só depois diz se
entregou. Não dá pra ter certeza NO ato. Mas dá pra saber que a linha está
recusando TUDO agora — e aí avisar antes é melhor que mentir depois.

Escopo de propósito estreito: só falha de COBRANÇA, que é persistente (não
melhora sozinha em minutos) e tem contorno óbvio — a corretora fala pelo
celular dela, que em coexistência é WhatsApp normal, sem API e sem cobrança.
Falha pontual de entrega (número errado, aparelho sem rede) NÃO entra aqui:
essa vale tentar de novo.
"""
from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import text as sql_text

log = structlog.get_logger()

# Como a Meta escreve o erro de pagamento hoje. Casado por ILIKE porque o texto
# já mudou de forma antes ("payment"/"eligibility") e prender na string exata
# faria o guard sumir sem ninguém perceber.
_PADRAO_COBRANCA = "%payment%"

# Quanto tempo pra trás procurar a recusa. Largo de propósito: cobrança travada
# não se resolve sozinha, e as falhas chegam ESPARSAS (a linha da Pessoa10 levou 3
# em 2 horas). Contar ocorrências numa janela curta não pegava nada.
_JANELA_H = 24


def bloqueada_por_cobranca(
    session: Any, instance: str | None, *, janela_h: int = _JANELA_H
) -> bool:
    """True se a última notícia da linha é recusa de COBRANÇA.

    A regra não é "quantas falhas": é **ordem**. Bloqueia quando houve recusa de
    pagamento e, DEPOIS dela, nenhum template foi entregue por essa linha. No
    instante em que um template volta a entregar, o guard sai do caminho sozinho
    — sem esperar janela expirar.

    Só TEMPLATE conta como prova de vida, de propósito: é o caminho pago, o
    mesmo que a cobrança derruba. Texto livre do CELULAR da corretora
    (coexistência) é entregue mesmo com a conta travada — contar isso como
    sucesso faria o guard desligar justamente quando ela usa o contorno.

    Fail-OPEN: erro de consulta devolve False. Este guard só existe pra avisar —
    se ele quebrar, não pode ser o motivo de a corretora não falar com o cliente.
    """
    if not instance:
        return False
    try:
        row = session.execute(
            sql_text(
                """
                SELECT
                  (SELECT max(timestamp_evolution) FROM mensagens_whatsapp
                    WHERE instance = :inst
                      AND conteudo_metadata->>'delivery_status' = 'failed'
                      AND conteudo_metadata->>'delivery_error' ILIKE :pat
                      AND timestamp_evolution > now() - (:h || ' hours')::interval
                  ) AS ultima_recusa,
                  (SELECT max(timestamp_evolution) FROM mensagens_whatsapp
                    WHERE instance = :inst
                      AND tipo_mensagem = 'template'
                      AND conteudo_metadata->>'delivery_status' IN ('delivered', 'read')
                      AND timestamp_evolution > now() - (:h || ' hours')::interval
                  ) AS ultimo_sucesso
                """
            ),
            {"inst": instance, "pat": _PADRAO_COBRANCA, "h": int(janela_h)},
        ).mappings().first()
    except Exception:  # noqa: BLE001
        log.warning("linha_saude_indisponivel", instance=instance, exc_info=True)
        return False
    if row is None or row["ultima_recusa"] is None:
        return False
    sucesso = row["ultimo_sucesso"]
    return sucesso is None or sucesso < row["ultima_recusa"]
