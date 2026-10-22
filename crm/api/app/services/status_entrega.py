"""Status de entrega da Meta (sent / delivered / read / failed) — aplicar sem perder.

## Por que existe

O webhook de status fazia um `UPDATE … WHERE evolution_message_id = :wamid`,
não olhava quantas linhas pegou e respondia `ok`. Dois jeitos de o status
sumir calado:

1. **A mensagem ainda não está gravada** (o status chega antes do registro da
   saída, ou o registro falhou): o UPDATE pega zero linhas e o status some. O
   placar de entrega fica "em voo" pra sempre.
2. **Fora de ordem**: `read` chega, depois chega um `sent` atrasado e
   sobrescreve — a mensagem lida volta a parecer não entregue.

Agora: status de wamid desconhecido vai pra `whatsapp_status_orfao` (migration
0090) e o conferente reaplica quando a mensagem aparecer; status mais velho
que o já gravado é ignorado, com log.

Sem rede, sem IA: só banco.
"""
from __future__ import annotations

import json
from typing import Any

import structlog
from sqlalchemy import text as sql_text

log = structlog.get_logger()

# Quanto cada status "vale". `failed` é terminal como `read`: nenhum dos dois é
# rebaixado por um `sent`/`delivered` que chegou atrasado.
_ORDEM = {"sent": 1, "delivered": 2, "read": 3, "failed": 3}

APLICADO = "aplicado"
ORFAO = "orfao"
FORA_DE_ORDEM = "fora_de_ordem"


def _valor(status: str | None) -> int:
    return _ORDEM.get((status or "").strip().lower(), 0)


def aplicar(session: Any, wamid: str, patch: dict[str, Any],
            linha: str | None = None, *, guardar_orfao: bool = True) -> str:
    """Aplica o patch de status na mensagem do `wamid`. Não commita.

    Devolve `APLICADO`, `ORFAO` (não há mensagem com esse id — guardado pra
    reaplicar) ou `FORA_DE_ORDEM` (o que está gravado é mais novo).
    """
    novo = patch.get("delivery_status")
    atual = session.execute(
        sql_text(
            "SELECT conteudo_metadata->>'delivery_status' FROM mensagens_whatsapp "
            " WHERE evolution_message_id = :w "
            " ORDER BY (conteudo_metadata ? 'delivery_status') DESC LIMIT 1"
        ),
        {"w": wamid},
    ).first()
    if atual is None:
        if guardar_orfao:
            session.execute(
                sql_text(
                    """INSERT INTO whatsapp_status_orfao (wamid, linha, status, patch)
                       VALUES (:w, :l, :s, CAST(:p AS jsonb))"""
                ),
                {"w": wamid, "l": linha, "s": novo, "p": json.dumps(patch)},
            )
            log.warning("status_de_entrega_sem_mensagem_guardado", linha=linha,
                        status=novo, wamid_fim=wamid[-6:])
        return ORFAO
    if _valor(novo) < _valor(atual[0]):
        log.info("status_de_entrega_fora_de_ordem_ignorado", linha=linha,
                 gravado=atual[0], chegou=novo, wamid_fim=wamid[-6:])
        return FORA_DE_ORDEM
    session.execute(
        sql_text(
            """UPDATE mensagens_whatsapp
                  SET conteudo_metadata =
                      coalesce(conteudo_metadata, '{}'::jsonb) || CAST(:patch AS jsonb)
                WHERE evolution_message_id = :w"""
        ),
        {"patch": json.dumps(patch), "w": wamid},
    )
    return APLICADO


def reaplicar_orfaos(session: Any, limite: int = 500) -> dict[str, int]:
    """Tenta de novo os status guardados cuja mensagem já existe. Commita.

    Órfão que continua sem mensagem fica onde está (e conta em `pendentes`): é
    status de mensagem que este sistema nunca gravou — vale saber que existe.
    """
    linhas = session.execute(
        sql_text(
            """SELECT o.id, o.wamid, o.linha, o.patch
                 FROM whatsapp_status_orfao o
                WHERE o.aplicado_em IS NULL
                  AND EXISTS (SELECT 1 FROM mensagens_whatsapp m
                               WHERE m.evolution_message_id = o.wamid)
                ORDER BY o.recebido_em, o.id
                LIMIT :lim"""
        ),
        {"lim": int(limite)},
    ).all()
    aplicados = 0
    for oid, wamid, linha, patch in linhas:
        if isinstance(patch, str):
            patch = json.loads(patch)
        r = aplicar(session, wamid, dict(patch or {}), linha, guardar_orfao=False)
        if r in (APLICADO, FORA_DE_ORDEM):
            session.execute(
                sql_text("UPDATE whatsapp_status_orfao SET aplicado_em = now() WHERE id = :i"),
                {"i": oid},
            )
            aplicados += 1
    session.commit()
    pendentes = int(session.execute(
        sql_text("SELECT count(*) FROM whatsapp_status_orfao WHERE aplicado_em IS NULL")
    ).scalar() or 0)
    return {"reaplicados": aplicados, "pendentes": pendentes}
