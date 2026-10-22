"""
Dispatcher — dispara HSM Meta Cloud API pra leads na etapa "Entrada" do
funil Remarketing.

Por lead:
  1. Valida telefone, busca em recovery_conversations (idempotência)
  2. Envia template HSM via Cloud API (com {{1}}=primeiro nome se configurado)
  3. Cria recovery_conversation em estado AGUARDANDO + hsm_sent_at=now
  4. Move o lead pra etapa "API" no Kommo

Rate-limit: sleep entre disparos pra evitar throttle Meta.
DRY_RUN: loga sem chamar Cloud API e sem mover stage Kommo.
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.services.emission_gate import guarded

import httpx
import structlog

from app.config import settings
from app.db.session import SessionLocal
from app.integrations.kommo_client import KommoClient
from app.recovery import repo
from app.services.reengajamento_guard import cadencia_engajada
from app.tools.kommo import KommoTool, _phone_from_contact

log = structlog.get_logger()


@dataclass
class DispatchResult:
    total_candidates: int = 0
    sent: int = 0
    already_in_recovery: int = 0
    skipped_cadencia: int = 0
    skipped_no_phone: int = 0
    failed: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)


def dispatch_wave(*, limit: int | None = None, dry_run: bool | None = None) -> DispatchResult:
    """
    Executa uma onda de disparo.

    Args:
        limit: máximo de leads por onda. Default = settings.recovery_dispatch_default_limit.
        dry_run: força DRY_RUN local (default = settings.dry_run).
    """
    limit = limit or settings.recovery_dispatch_default_limit
    dry = settings.dry_run if dry_run is None else dry_run

    _check_config(dry)

    result = DispatchResult()
    leads = _fetch_leads_entrada(limit)
    result.total_candidates = len(leads)

    log.info("recovery_dispatch_start", limit=limit, candidates=len(leads), dry_run=dry)

    for i, lead in enumerate(leads, 1):
        phone = lead.get("telefone") or ""
        lead_id = lead.get("lead_id") or ""
        name = lead.get("nome") or None

        if not phone or not lead_id:
            result.skipped_no_phone += 1
            log.info("recovery_dispatch_skip_no_phone", lead_id=lead_id)
            continue

        digits = _digits_only(phone)

        # TRAVA ANTI-COLISÃO (F1 do B9): lead ATIVO na cadência de 8 toques
        # (linha Pessoa12) não entra em onda da Corretor — já está sendo trabalhado por
        # outro motor. Fail-open: erro na trava NÃO bloqueia o disparo.
        if cadencia_engajada(digits):
            result.skipped_cadencia += 1
            log.info(
                "reengajamento_guard_bloqueou",
                motor="corretor_dispatcher", lead_id=lead_id,
                telefone_last4=digits[-4:],
            )
            continue

        with SessionLocal() as session:
            existing = repo.find_by_phone(session, digits)
            if existing is not None:
                result.already_in_recovery += 1
                log.info(
                    "recovery_dispatch_skip_existing",
                    lead_id=lead_id, state=existing.state,
                )
                continue
            # Claim atômico ANTES do envio (Codex #4): reserva a conversa por
            # lead_id. Se outra wave concorrente reservou primeiro, pulamos —
            # assim nunca despachamos um 2º HSM pro mesmo lead numa corrida.
            if not repo.claim_new(
                session, lead_id=lead_id, lead_phone=digits, lead_name=name
            ):
                result.already_in_recovery += 1
                log.info("recovery_dispatch_skip_claimed", lead_id=lead_id)
                continue

        ok = _send_hsm(digits, name, dry=dry)
        if not ok:
            result.failed += 1
            result.errors.append({"lead_id": lead_id, "phone_last4": digits[-4:]})
            continue

        with SessionLocal() as session:
            row = repo.get_or_create(
                session, lead_id=lead_id, lead_phone=digits, lead_name=name
            )
            row.hsm_sent_at = datetime.now(timezone.utc)
            session.commit()

        _move_to_api_stage(lead_id, dry=dry)
        result.sent += 1

        if i < len(leads) and settings.recovery_dispatch_delay_ms > 0:
            time.sleep(settings.recovery_dispatch_delay_ms / 1000)

    log.info(
        "recovery_dispatch_done",
        total=result.total_candidates, sent=result.sent,
        already=result.already_in_recovery, skipped=result.skipped_no_phone,
        skipped_cadencia=result.skipped_cadencia,
        failed=result.failed,
    )
    return result


# ── internals ────────────────────────────────────────────────────────────────

def _check_config(dry: bool) -> None:
    missing = []
    if not settings.kommo_pipeline_id_remarketing:
        missing.append("kommo_pipeline_id_remarketing")
    if not settings.kommo_stage_remarketing_entrada:
        missing.append("kommo_stage_remarketing_entrada")
    if not settings.kommo_stage_remarketing_api:
        missing.append("kommo_stage_remarketing_api")
    if not dry:
        if not settings.meta_cloud_phone_number_id:
            missing.append("meta_cloud_phone_number_id")
        if not settings.meta_cloud_access_token:
            missing.append("meta_cloud_access_token")
        if not settings.recovery_hsm_template_name:
            missing.append("recovery_hsm_template_name")
    if missing:
        raise RuntimeError(f"Dispatcher: config faltando: {', '.join(missing)}")


def _fetch_leads_entrada(limit: int) -> list[dict[str, Any]]:
    """Lista leads na etapa Entrada do funil Remarketing (com telefone).

    Kommo não devolve custom_fields nos contatos embutidos no lead — precisa
    de uma chamada separada GET /contacts?with=custom_fields. Faço isso em
    batch (1 call pra todos os contatos da onda).
    """
    client = KommoClient(subdomain=settings.kommo_subdomain, token=settings.kommo_token)
    filters = {
        "filter[pipeline_id]": settings.kommo_pipeline_id_remarketing,
        "filter[statuses][0][pipeline_id]": settings.kommo_pipeline_id_remarketing,
        "filter[statuses][0][status_id]": settings.kommo_stage_remarketing_entrada,
    }
    leads = list(itertools.islice(client.get_leads(filters), limit))

    contact_ids: list[int] = []
    lead_contact_map: dict[str, int] = {}
    for lead in leads:
        lid = str(lead.get("id", ""))
        contacts = lead.get("_embedded", {}).get("contacts", [])
        if contacts:
            cid = int(contacts[0].get("id"))
            contact_ids.append(cid)
            lead_contact_map[lid] = cid

    enriched = client.get_contacts_by_ids(contact_ids) if contact_ids else []
    phone_by_contact: dict[int, str] = {
        int(c["id"]): _phone_from_contact(c) for c in enriched
    }

    flat: list[dict[str, Any]] = []
    for lead in leads:
        lid = str(lead.get("id", ""))
        cid = lead_contact_map.get(lid)
        phone = phone_by_contact.get(cid, "") if cid else ""
        flat.append({
            "lead_id": lid,
            "nome": lead.get("name", ""),
            "telefone": phone,
        })
    return flat


def _digits_only(phone: str) -> str:
    return "".join(c for c in phone if c.isdigit())


@guarded
def _send_hsm(phone_digits: str, lead_name: str | None, *, dry: bool) -> bool:
    """Envia template HSM via Meta Cloud API. Retorna True em sucesso."""
    if dry:
        log.info(
            "recovery_hsm_dry_run",
            phone_last4=phone_digits[-4:],
            template=settings.recovery_hsm_template_name or "(none)",
            name=lead_name,
        )
        return True

    url = (
        f"https://graph.facebook.com/{settings.meta_cloud_api_version}"
        f"/{settings.meta_cloud_phone_number_id}/messages"
    )
    headers = {
        "Authorization": f"Bearer {settings.meta_cloud_access_token}",
        "Content-Type": "application/json",
    }

    template_payload: dict[str, Any] = {
        "name": settings.recovery_hsm_template_name,
        "language": {"code": settings.recovery_hsm_language},
    }
    if settings.recovery_hsm_has_name_param:
        first_name = _first_name(lead_name) or "tudo bem"
        template_payload["components"] = [{
            "type": "body",
            "parameters": [{"type": "text", "text": first_name}],
        }]

    body = {
        "messaging_product": "whatsapp",
        "to": phone_digits,
        "type": "template",
        "template": template_payload,
    }

    try:
        with httpx.Client(timeout=20) as client:
            resp = client.post(url, headers=headers, json=body)
            if resp.status_code >= 400:
                log.error(
                    "recovery_hsm_failed",
                    phone_last4=phone_digits[-4:],
                    status=resp.status_code, body=resp.text[:300],
                )
                return False
            log.info(
                "recovery_hsm_sent",
                phone_last4=phone_digits[-4:],
                wa_msg_id=_extract_wa_id(resp.json()),
            )
            return True
    except Exception as exc:
        log.error(
            "recovery_hsm_exception",
            phone_last4=phone_digits[-4:], error=str(exc),
        )
        return False


def _extract_wa_id(resp_json: dict) -> str | None:
    msgs = resp_json.get("messages") or []
    if msgs and isinstance(msgs, list):
        return msgs[0].get("id")
    return None


def _first_name(full: str | None) -> str | None:
    if not full:
        return None
    return full.strip().split()[0] if full.strip() else None


def _move_to_api_stage(lead_id: str, *, dry: bool) -> None:
    if dry:
        # DRY-RUN não pode ter efeito externo: NÃO mover o lead no Kommo
        # (antes o parâmetro dry era ignorado e o funil mudava de verdade
        # numa simulação, podendo impedir o disparo operacional real — Codex #4).
        log.info("recovery_dispatch_move_dry_run", lead_id=lead_id)
        return
    kommo = KommoTool()
    try:
        kommo.move_lead_stage(
            lead_id,
            int(settings.kommo_stage_remarketing_api),
            pipeline_id=settings.kommo_pipeline_id_remarketing or None,
        )
    except Exception as exc:
        log.error("recovery_dispatch_move_failed", lead_id=lead_id, error=str(exc))
