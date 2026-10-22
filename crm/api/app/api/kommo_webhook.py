"""Webhook receiver pro Kommo — dispara anúncio Alexa em lead novo.

Configurar no Kommo: Settings → Integrations → Webhooks
URL: https://<host>/webhook/kommo/lead-created/<KOMMO_WEBHOOK_SECRET>
Eventos: Add lead (no funil principal)
"""
from __future__ import annotations

from typing import Any, Callable

import structlog
from fastapi import APIRouter, Body, Path, Request, Response
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Corretora
from app.integrations.alexa import announce
from app.services.phone import normalize_phone_kommo

log = structlog.get_logger()

router = APIRouter(prefix="/webhook/kommo", tags=["webhook"])


def _extract_lead_name(payload: dict[str, Any]) -> str | None:
    """Kommo manda form-encoded ou JSON. Extrai nome do primeiro lead 'add'."""
    first = _first_added_lead(payload)
    return first.get("name") or None if first else None


def _first_added_lead(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Retorna o primeiro lead do bloco leads[add], ou None."""
    leads = payload.get("leads") or {}
    add_list = leads.get("add") or []
    if not add_list:
        return None
    return add_list[0] or {}


def _kommo_lead_to_shadow(first: dict[str, Any]) -> dict[str, Any]:
    """Mapeia o payload de lead do webhook Kommo pro contrato do lead_spine.

    Campos do Kommo (add lead): id, name, status_id (etapa), responsible_user_id.
    O webhook é fino — telefone/origem podem não vir; o shadow tolera ausência
    e o próximo passe do Inspector completa.
    """
    return {
        "lead_id": first.get("id"),
        "nome": first.get("name"),
        "etapa_id": first.get("status_id"),
        "owner_id": first.get("responsible_user_id"),
    }


def _extract_lead_phone(first: dict[str, Any]) -> str | None:
    """Extrai o telefone cru do payload do webhook, se veio.

    O webhook de lead novo do Kommo é fino — telefone pode não vir. Quando vem,
    chega nos custom fields. Tolera os dois formatos que o Kommo usa
    (`custom_fields` no webhook, `custom_fields_values` na API) e casa por
    code=PHONE (como app/tools/kommo.py) ou pelo nome do campo.
    """
    for container in ("custom_fields", "custom_fields_values"):
        for field in first.get(container) or []:
            if not isinstance(field, dict):
                continue
            code = str(field.get("code") or field.get("field_code") or "").upper()
            name = str(field.get("name") or field.get("field_name") or "").strip().lower()
            if code != "PHONE" and name not in ("telefone", "phone", "fone", "celular"):
                continue
            for v in field.get("values") or []:
                value = v.get("value") if isinstance(v, dict) else v
                if value:
                    return str(value)
    return None


def _normalizar_telefone_kommo(raw: str | None) -> str | None:
    """Delegado pra `app.services.phone.normalize_phone_kommo` (fonte única —
    o lead_spine usa a mesma normalização no enroll-on-followup).

    Retorna None se inválido (nunca levanta — o webhook não pode quebrar).
    """
    return normalize_phone_kommo(raw)


def _auto_enroll_cadencia_corretor(
    first: dict[str, Any],
    session_factory: Callable[[], Session] | None = None,
) -> int | None:
    """Matricula o lead novo na cadência automática — SÓ linha da Pessoa12 (Cloud 2591).

    Atrás da flag `settings.cadencia_auto_enroll` (default False — deploy seguro).
    Identificação da Pessoa12: pela tabela `corretoras`, casando o responsible_user_id
    do payload com `corretoras.kommo_user_id` da corretora cuja evolution_instance
    é a da linha Cloud (`cloud_corretora_a.INSTANCE`). No Kommo a conta dela aparece
    como "Pessoa Pessoa9" — por isso NUNCA casar por nome.

    Idempotente: `iniciar_cadencia` devolve a cadência existente por
    (tenant, telefone) sem duplicar (UNIQUE no schema garante por baixo).

    Retorna o id da cadência (nova ou existente) ou None se não matriculou.
    """
    if not settings.cadencia_auto_enroll:
        return None

    try:
        owner_id = int(first.get("responsible_user_id"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        log.info("cadencia_auto_enroll_skip", motivo="sem_owner", lead_id=first.get("id"))
        return None

    telefone = _normalizar_telefone_kommo(_extract_lead_phone(first))
    if telefone is None:
        log.info(
            "cadencia_auto_enroll_skip", motivo="sem_telefone_valido", lead_id=first.get("id")
        )
        return None

    # Imports tardios: cloud_corretora_a puxa o registry de linhas Cloud e
    # SessionLocal cria engine — nada disso pode pesar no import do webhook.
    from app.api.cloud_corretora_a import INSTANCE as CORRETOR_INSTANCE
    from app.services.cadencia import iniciar_cadencia
    from app.services.reengajamento_guard import corretor_conversa_ativa

    if session_factory is None:
        from app.db.session import SessionLocal

        session_factory = SessionLocal

    with session_factory() as session:
        corretor = (
            session.query(Corretora)
            .filter(
                Corretora.evolution_instance == CORRETOR_INSTANCE,
                Corretora.ativo.is_(True),
                Corretora.kommo_user_id.isnot(None),
            )
            .first()
        )
        if corretor is None or int(corretor.kommo_user_id) != owner_id:
            log.info("cadencia_auto_enroll_skip", motivo="nao_e_corretor", owner_id=owner_id)
            return None

        lead_id = first.get("id")

        # TRAVA ANTI-COLISÃO (F1 do B9): lead em conversa VIVA com a Corretor
        # (recovery) NÃO é matriculado agora — o enroll natural acontece
        # depois (move pro Follow-up / auto-followup), quando a conversa da
        # Corretor tiver esfriado. Fail-open: erro na trava não impede o enroll.
        if corretor_conversa_ativa(telefone, session=session):
            log.info(
                "reengajamento_guard_bloqueou",
                motor="auto_enroll", lead_id=lead_id,
                telefone_last4=telefone[-4:],
            )
            return None

        estado = iniciar_cadencia(
            session,
            telefone=telefone,
            lead_id=str(lead_id) if lead_id is not None else None,
            corretora_id=corretor.id,
        )
        session.commit()
        log.info(
            "cadencia_auto_enroll_ok",
            cadencia_id=estado.id,
            lead_id=lead_id,
            telefone=telefone[-4:],
        )
        return estado.id


def _status_changed_leads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Retorna os leads do bloco leads[status] (webhook "Lead stage changed")."""
    leads = payload.get("leads") or {}
    return [l or {} for l in (leads.get("status") or [])]


def _auto_enroll_cadencia_followup_corretor(
    first: dict[str, Any],
    session_factory: Callable[[], Session] | None = None,
) -> int | None:
    """Enroll-on-followup: lead (da Pessoa12) MOVIDO pra etapa Follow-up do Kommo →
    matricula na cadência. Mesma flag/idempotência do `_auto_enroll_cadencia_corretor`
    (que faz todo o trabalho — aqui só o gate da etapa).

    A cadência é invisível: nenhum módulo próprio — quem move o card pro
    Follow-up no CRM liga a máquina; o toque atual aparece como tag CD{n}.
    """
    if not settings.cadencia_auto_enroll:
        return None

    # Import tardio (mesmo motivo do auto-enroll): cloud_corretora_a é pesado.
    from app.api.cloud_corretora_a import KOMMO_STAGE_FOLLOWUP

    try:
        status_id = int(first.get("status_id"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if status_id != KOMMO_STAGE_FOLLOWUP:
        return None

    return _auto_enroll_cadencia_corretor(first, session_factory=session_factory)


@router.post("/lead-created/{secret}")
async def kommo_lead_created(
    request: Request,
    secret: str = Path(...),
) -> Response:
    if not settings.kommo_webhook_secret or secret != settings.kommo_webhook_secret:
        return Response(status_code=404)

    # Kommo envia form-encoded por padrão; tolera JSON também.
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        payload = await request.json()
    else:
        form = await request.form()
        payload = _form_to_dict(form)

    lead_name = _extract_lead_name(payload)
    log.info("kommo_webhook_lead_created", lead_name=lead_name)

    # ── Dual-write EM SOMBRA da espinha canônica (Opção A) ────────────────────
    # Best-effort: falha aqui NÃO pode quebrar o webhook (senão o Kommo re-tenta
    # e/ou a Alexa não anuncia). Leitura segue no Kommo; só gravamos em paralelo.
    first = _first_added_lead(payload)
    if first:
        try:
            from app.services.lead_spine import upsert_lead_shadow
            upsert_lead_shadow(_kommo_lead_to_shadow(first))
        except Exception:
            log.exception("lead_spine_shadow_failed", origem="kommo_webhook")

        # ── Auto-enroll na cadência (linha da Pessoa12) — best-effort, atrás de flag ──
        # Falha na matrícula NUNCA derruba o webhook (mesmo padrão do lead_spine).
        try:
            _auto_enroll_cadencia_corretor(first)
        except Exception:
            log.exception("cadencia_auto_enroll_failed", origem="kommo_webhook")

    # ── Enroll-on-followup: lead movido pra etapa Follow-up (webhook de status) ─
    # O Kommo manda "Lead stage changed" em leads[status] (mesma URL). Best-effort.
    for moved in _status_changed_leads(payload):
        try:
            _auto_enroll_cadencia_followup_corretor(moved)
        except Exception:
            log.exception("cadencia_enroll_followup_failed", origem="kommo_webhook")

    text = f"Caiu um lead novo na Acme: {lead_name}" if lead_name else "Caiu um lead novo na Acme"
    await announce(text)

    return Response(status_code=200)


def _form_to_dict(form: Any) -> dict[str, Any]:
    """Converte form Kommo (leads[add][0][name]=Foo) em dict aninhado."""
    out: dict[str, Any] = {}
    for raw_key, value in form.multi_items():
        parts = _parse_key(raw_key)
        _set_nested(out, parts, value)
    return out


def _parse_key(key: str) -> list[str | int]:
    out: list[str | int] = []
    buf = ""
    in_bracket = False
    for ch in key:
        if ch == "[":
            if buf:
                out.append(buf)
                buf = ""
            in_bracket = True
        elif ch == "]":
            if buf:
                out.append(int(buf) if buf.isdigit() else buf)
                buf = ""
            in_bracket = False
        else:
            buf += ch
    if buf:
        out.append(buf)
    return out


def _set_nested(d: dict[str, Any], parts: list[str | int], value: Any) -> None:
    cur: Any = d
    for i, p in enumerate(parts):
        last = i == len(parts) - 1
        if isinstance(p, int):
            while len(cur) <= p:
                cur.append({})
            if last:
                cur[p] = value
            else:
                cur = cur[p]
        else:
            nxt = parts[i + 1] if not last else None
            if last:
                cur[p] = value
            else:
                if p not in cur:
                    cur[p] = [] if isinstance(nxt, int) else {}
                cur = cur[p]
