"""Recado avulso pra uma corretora, entregue quando ela abre a janela de 24h.

O push de briefing (`briefing_push`) manda um template UTILITY de aviso; o
clique em "Saiba mais" abre a janela e o conteúdo sai como texto livre, de
graça. Até aqui o clique só sabia entregar o briefing gerado do dia.

Recado é outra coisa: um fato pontual ("ficaram 4 pessoas sem resposta hoje"),
escrito na hora, que não cabe no formato do briefing e não pode esperar até
segunda. Fluxo:

    criar(corretora_id, texto, autor)   → guarda o recado pendente
    briefing_push.enviar_aviso_briefing → template de aviso (fora da janela)
    corretora toca "Saiba mais"         → handler chama entregar()
    entregar()                          → manda o recado e marca enviado

Um pendente por corretora (índice parcial na migration 0076): criar um novo
cancela o anterior. Recado velho entregue dias depois é pior que recado nenhum
— a corretora age em cima de um dia que já passou.

A entrega NÃO tem transporte próprio: reusa `enviar_cloud_assistente`, o mesmo do
briefing completo, que respeita CLOUD_DRY_RUN e a guarda de pytest.
"""
from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import text as sql_text

from app.db.session import SessionLocal

log = structlog.get_logger()


def criar(*, corretora_id: int, texto: str, autor: str, tenant_id: int = 1) -> int:
    """Guarda o recado pendente, cancelando o anterior da mesma corretora."""
    texto = (texto or "").strip()
    if not texto:
        raise ValueError("recado vazio")
    with SessionLocal() as s:
        s.execute(
            sql_text(
                """UPDATE recado_corretora SET cancelado_em = NOW()
                    WHERE tenant_id = :t AND corretora_id = :c
                      AND enviado_em IS NULL AND cancelado_em IS NULL"""
            ),
            {"t": tenant_id, "c": corretora_id},
        )
        rid = s.execute(
            sql_text(
                """INSERT INTO recado_corretora (tenant_id, corretora_id, texto,
                                                 criado_por)
                   VALUES (:t, :c, :x, :a) RETURNING id"""
            ),
            {"t": tenant_id, "c": corretora_id, "x": texto, "a": autor},
        ).scalar_one()
        s.commit()
    log.info("recado_criado", corretora_id=corretora_id, autor=autor, id=rid,
             chars=len(texto))
    return int(rid)


def pendente(corretora_id: int, tenant_id: int = 1) -> dict[str, Any] | None:
    with SessionLocal() as s:
        r = s.execute(
            sql_text(
                """SELECT id, texto, criado_por, criado_em
                     FROM recado_corretora
                    WHERE tenant_id = :t AND corretora_id = :c
                      AND enviado_em IS NULL AND cancelado_em IS NULL
                    ORDER BY id DESC LIMIT 1"""
            ),
            {"t": tenant_id, "c": corretora_id},
        ).mappings().first()
    return dict(r) if r else None


def entregar(corretora_id: int, destino_phone: str,
             tenant_id: int = 1) -> dict[str, Any] | None:
    """Manda o recado pendente pela janela aberta. None = não havia recado.

    Só marca `enviado_em` se o envio deu certo — falha de rede não pode comer
    o recado calado, senão a corretora toca o botão, não recebe nada, e o
    aviso já foi gasto.
    """
    from app.api.cloud_assistente import enviar_cloud_assistente  # import tardio (ciclo)

    r = pendente(corretora_id, tenant_id)
    if not r:
        return None

    res = enviar_cloud_assistente(destino_phone, r["texto"])
    if res.get("ok"):
        with SessionLocal() as s:
            s.execute(
                sql_text("UPDATE recado_corretora SET enviado_em = NOW() WHERE id = :i"),
                {"i": r["id"]},
            )
            s.commit()
        try:
            from app.services.conversations import append_message

            append_message(phone=destino_phone, sender_kind="assistant",
                           sender_id=None, message_role="assistant",
                           text=r["texto"])
        except Exception as e:  # noqa: BLE001 — best-effort, igual ao briefing
            log.warning("recado_persist_falhou", erro=str(e)[:160])

    log.info("recado_entregue", corretora_id=corretora_id, id=r["id"],
             ok=res.get("ok"), sent=res.get("sent"),
             dry_run=res.get("dry_run", False))
    return {**res, "recado_id": r["id"], "message_length": len(r["texto"])}
