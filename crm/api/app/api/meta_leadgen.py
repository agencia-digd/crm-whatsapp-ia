"""Receptor Meta leadgen em MODO SOMBRA (B6) — POST /intake/meta-leadgen/{secret}.

Recebe o body ORIGINAL do webhook de Lead Ads da Meta (formato entry/changes,
field="leadgen"), repassado pelo automacao-kommo (que continua sendo o callback
oficial do app 0000 e continua criando o lead no Kommo). Handler fino: autentica
pelo secret do path (fail-closed), itera os changes e delega pro serviço
`app.services.meta_leadgen.processar_leadgen_value` (sombra SEMPRE; intake só
com `LEAD_INTAKE_ATIVO=true`).

Auth: path secret == settings.meta_leadgen_secret, comparação em tempo
constante. Secret não configurado = endpoint FECHADO (401) — mesmo padrão
fail-closed do intake.py / cloud_corretora_a.

Resposta NÃO ecoa nome/telefone — só leadgen_id + status (o forwarder loga a
resposta; dado pessoal fica no banco, não em log de terceiro).
"""
from __future__ import annotations

import hmac
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.services.meta_leadgen import processar_leadgen_value

log = structlog.get_logger()

router = APIRouter(prefix="/intake", tags=["intake"])


def _auth_meta_leadgen(secret_path: str) -> None:
    """Valida o secret do path (fail-closed, tempo constante)."""
    expected = settings.meta_leadgen_secret
    if not expected or not hmac.compare_digest(secret_path, expected):
        log.warning("meta_leadgen_auth_falha")
        raise HTTPException(401, "unauthorized")


class MetaLeadgenWebhookIn(BaseModel):
    """Body do webhook de Lead Ads da Meta (subset relevante)."""

    object: str | None = None
    entry: list[dict[str, Any]] = Field(default_factory=list)


# `def` (sync) de propósito: serviço é SQLAlchemy + httpx síncronos — FastAPI
# roteia pro threadpool e o event loop não trava (gotcha da casa §4.1).
@router.post("/meta-leadgen/{secret}")
def receber_meta_leadgen(secret: str, payload: MetaLeadgenWebhookIn) -> dict[str, Any]:
    _auth_meta_leadgen(secret)

    resultados: list[dict[str, Any]] = []
    for entry in payload.entry:
        for change in entry.get("changes", []):
            if change.get("field") != "leadgen":
                continue
            value = change.get("value") or {}
            try:
                resultados.append(processar_leadgen_value(value))
            except Exception as exc:
                # Nunca 500 pro forwarder — o fluxo Kommo dele não pode sofrer.
                log.exception("meta_leadgen_processar_falhou", erro=str(exc)[:200])
                resultados.append(
                    {"leadgen_id": value.get("leadgen_id"), "status": "erro"}
                )

    log.info("meta_leadgen_recebido", n=len(resultados))
    return {"ok": True, "processados": len(resultados), "resultados": resultados}
