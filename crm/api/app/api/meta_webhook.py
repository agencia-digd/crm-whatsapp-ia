"""Receptor REAL do webhook de Lead Ads da Meta — GET+POST /intake/meta-webhook.

Callback DIRETO da Meta (intake próprio, pra aposentar o Kommo). Duas rotas:

  • GET  — verificação do webhook. A Meta chama com hub.mode=subscribe,
    hub.verify_token e hub.challenge; se o verify_token bate com
    `settings.intake_meta_verify_token` (fail-closed: vazio = recusa), devolve
    o hub.challenge cru (text/plain). FUNCIONA mesmo com a flag lead_intake_ativo
    OFF — senão não dá pra assinar o webhook no painel da Meta.

  • POST — entrega de eventos. Valida X-Hub-Signature-256 (HMAC-SHA256 do corpo
    cru com `settings.intake_meta_app_secret`, fail-closed: vazio = 403). Com a
    flag lead_intake_ativo OFF, responde 200 "desligado" SEM processar (padrão
    webhook: nunca 500/erro pra Meta não desativar a assinatura). Com a flag ON,
    itera os changes field='leadgen' e delega pro serviço
    `processar_evento_leadgen` (Graph API → intake_lead).

Handler POST é `async` só pra ler o corpo CRU (request.body()) — a assinatura
tem que bater sobre os bytes exatos, não sobre o JSON reserializado. O
processamento em si é síncrono (SQLAlchemy/httpx); roda no threadpool via
run_in_threadpool pra não travar o event loop.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import structlog
from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.services import config_operacional
from app.services.meta_webhook import processar_evento_leadgen

log = structlog.get_logger()

router = APIRouter(prefix="/intake", tags=["intake"])


@router.get("/meta-webhook")
def verificar_meta_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> Response:
    """Verificação do webhook (handshake da Meta).

    Fail-closed: `intake_meta_verify_token` vazio = recusa (403). Token errado
    ou modo != subscribe = 403. OK = devolve o challenge cru (text/plain), como
    a Meta exige.
    """
    expected = settings.intake_meta_verify_token
    if (
        not expected
        or hub_mode != "subscribe"
        or not hub_verify_token
        or not hmac.compare_digest(hub_verify_token, expected)
    ):
        log.warning("meta_webhook_verify_falha", mode=hub_mode)
        raise HTTPException(403, "forbidden")
    log.info("meta_webhook_verify_ok")
    # A Meta espera o challenge cru no corpo (não JSON).
    return Response(content=hub_challenge or "", media_type="text/plain")


def _assinatura_valida(body: bytes, signature_header: str | None) -> bool:
    """Valida X-Hub-Signature-256 (HMAC-SHA256 do corpo cru com o app secret).

    Fail-closed: app secret vazio → inválido. Comparação em tempo constante.
    Header no formato 'sha256=<hexdigest>'.
    """
    secret = settings.intake_meta_app_secret
    if not secret or not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    enviado = signature_header.split("=", 1)[1]
    esperado = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(enviado, esperado)


@router.post("/meta-webhook")
async def receber_meta_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
) -> dict[str, Any]:
    """Entrega de eventos leadgen (webhook REAL da Meta)."""
    body = await request.body()

    # Assinatura primeiro (mesmo com a flag off — payload forjado não passa).
    if not _assinatura_valida(body, x_hub_signature_256):
        log.warning("meta_webhook_assinatura_invalida")
        raise HTTPException(403, "invalid signature")

    # Flag off = deploy seguro. Responde 200 sem processar (Meta não pode receber
    # erro, senão desativa a assinatura).
    if not config_operacional.lead_intake_ativo():
        log.info("meta_webhook_flag_off_200")
        return {"ok": True, "status": "desligado", "processados": 0}

    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        # Corpo inválido: assinatura bateu, mas não é JSON. 200 defensivo.
        log.warning("meta_webhook_body_nao_json")
        return {"ok": True, "status": "body_invalido", "processados": 0}

    resultados: list[dict[str, Any]] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            if change.get("field") != "leadgen":
                continue
            value = change.get("value") or {}
            try:
                # Serviço é síncrono (SQLAlchemy/httpx) — threadpool.
                res = await run_in_threadpool(processar_evento_leadgen, value)
                resultados.append(res)
            except Exception as exc:
                # Nunca 500 pra Meta — só loga e segue.
                log.exception("meta_webhook_processar_falhou", erro=str(exc)[:200])
                resultados.append(
                    {"leadgen_id": value.get("leadgen_id"), "status": "erro"}
                )

    log.info("meta_webhook_recebido", n=len(resultados))
    return {"ok": True, "processados": len(resultados), "resultados": resultados}
