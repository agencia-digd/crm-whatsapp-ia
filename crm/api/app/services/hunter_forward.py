"""Porteiro → Hunter: encaminha mensagem de não-corretora pro serviço do Hunter.

DORMENTE até o cutover: nada importa este módulo ainda. No cutover, o
evolution_webhook passa a chamar forward_to_hunter() pra todo phone que NÃO é
corretora/dona conhecida, em vez de manter o cérebro do Hunter dentro do copiloto.

Config por env (sem mexer no config.py do copiloto):
  HUNTER_SERVICE_URL     default http://127.0.0.1:8011
  HUNTER_INBOUND_SECRET  mesmo segredo do service/.env (INBOUND_SECRET)
"""
from __future__ import annotations

import os

import httpx
import structlog

log = structlog.get_logger()


def forward_to_hunter(phone: str, text: str, message_id: str | None) -> bool:
    """Repassa a msg pro Hunter. Retorna True se o Hunter tratou (handled=true).

    Best-effort e rápido: timeout curto pra não segurar o webhook. O Hunter faz
    o trabalho pesado (LLM) no próprio processo.
    """
    base = os.environ.get("HUNTER_SERVICE_URL", "http://127.0.0.1:8011").rstrip("/")
    secret = os.environ.get("HUNTER_INBOUND_SECRET", "")
    if not secret:
        log.warning("hunter_forward_no_secret")
        return False
    try:
        r = httpx.post(
            f"{base}/webhook/inbound/{secret}",
            json={"phone": phone, "text": text, "message_id": message_id},
            timeout=90,
        )
        if r.status_code >= 300:
            log.warning("hunter_forward_bad_status", code=r.status_code)
            return False
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        return bool(data.get("handled"))
    except Exception as e:
        log.error("hunter_forward_failed", error=str(e), phone_last4=phone[-4:])
        return False
