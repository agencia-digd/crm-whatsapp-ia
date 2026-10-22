"""Endpoint de ENTRADA DO LEAD (Copiloto 2.0 · P0) — POST /intake/lead.

É aqui que o webhook Meta/Kommo vai apontar. O handler é fino: autentica
(token no header, fail-closed), valida o payload (Pydantic) e delega pro
serviço `app.services.lead_intake.intake_lead` (upsert espinha + roleta +
boas-vindas 1x + evento — SEM matrícula em cadência, lei Dona 10/07). Atrás
da flag de intake (`config_operacional.lead_intake_ativo()`, painel > .env;
default False).

Auth: header `X-Intake-Token` == settings.intake_token. Fail-closed — token
não configurado = endpoint FECHADO (401), mesmo padrão de
cloud_corretora_a._auth_cloud_webhook.
"""
from __future__ import annotations

import hmac

import structlog
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.services import config_operacional
from app.services.lead_intake import IntakeResult, intake_lead

log = structlog.get_logger()

router = APIRouter(prefix="/intake", tags=["intake"])


def _auth_intake(token_header: str | None) -> None:
    """Valida o token compartilhado do intake (header X-Intake-Token).

    Comparação em tempo constante (hmac.compare_digest) contra timing attack.
    Fail-closed: settings.intake_token vazio = endpoint FECHADO (401) — ninguém
    injeta lead forjado na espinha/cadência.
    """
    expected = settings.intake_token
    if not expected or not token_header or not hmac.compare_digest(token_header, expected):
        log.warning("intake_auth_falha")
        raise HTTPException(401, "unauthorized")


class IntakeLeadIn(BaseModel):
    """Payload do lead novo (webhook Meta/Kommo → intake)."""

    telefone: str = Field(min_length=8, description="Telefone do lead (qualquer formato BR)")
    nome: str | None = None
    origem: str | None = Field(default=None, description="Ex: 'meta_leadgen', 'kommo'")
    external_ref: str | None = Field(
        default=None, description="Id externo (kommo_id / leadgen_id Meta)"
    )


# `def` (sync) de propósito: o serviço é SQLAlchemy síncrono — FastAPI roteia
# pro threadpool e o event loop não trava (gotcha FastAPI async + blocking).
@router.post("/lead", response_model=IntakeResult)
def intake_lead_endpoint(
    payload: IntakeLeadIn,
    x_intake_token: str | None = Header(default=None, alias="X-Intake-Token"),
) -> IntakeResult:
    _auth_intake(x_intake_token)

    # Estado EFETIVO (painel > .env) — o interruptor da página de
    # boas-vindas grava em config_operacional.
    if not config_operacional.lead_intake_ativo():
        # Flag desligada = deploy seguro; o caller sabe que deve re-tentar depois.
        raise HTTPException(503, "lead intake desligado (LEAD_INTAKE_ATIVO=false)")

    try:
        resultado = intake_lead(
            nome=payload.nome,
            telefone=payload.telefone,
            origem=payload.origem,
            external_ref=payload.external_ref,
        )
    except ValueError as exc:  # telefone inválido/inormalizável
        raise HTTPException(422, str(exc)) from exc

    if resultado is None:  # defensivo (flag caiu entre o check e a chamada)
        raise HTTPException(503, "lead intake desligado (LEAD_INTAKE_ATIVO=false)")
    return resultado
