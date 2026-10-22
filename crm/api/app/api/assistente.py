"""Endpoints HTTP da Assistente — Onda 2."""
from __future__ import annotations

import asyncio
import threading
from typing import Any

import structlog
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.agents.assistente.agent import AgenteAssistente
from app.config import settings
from app.integrations.evolution_client import EvolutionClient
from app.services.conversations import append_message, is_duplicate, load_history
from app.services.phone import normalize_phone
from app.services.sender_lookup import identify_sender

log = structlog.get_logger()

router = APIRouter(prefix="/assistente", tags=["assistente"])

AGENT_TIMEOUT_S = 60


class _DeliveryGuard:
    """Garante entrega única da resposta da Assistente entre o caminho HTTP
    normal e o callback pós-timeout. claim() é compare-and-set atômico:
    o primeiro a chamar leva True e assume a entrega; os demais recebem
    False e viram no-op.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claimed = False

    def claim(self) -> bool:
        with self._lock:
            if self._claimed:
                return False
            self._claimed = True
            return True


def _deliver_late_response(phone: str, text: str) -> None:
    """Persiste + despacha a resposta que ficou pronta DEPOIS do timeout HTTP.

    Espelha app.services.assistente_dispatch._process_one na parte de entrega:
    grava a msg do assistant e envia via Evolution (instância assistente),
    respeitando dry_run. Best-effort — nunca levanta exceção.
    """
    if not text:
        return
    try:
        append_message(
            phone=phone, sender_kind="assistant", sender_id=None,
            message_role="assistant", text=text, message_id=None,
        )
    except Exception as e:  # noqa: BLE001
        log.error("assistente_late_persist_error", phone_last4=phone[-4:], erro=str(e))

    if settings.dry_run:
        log.info("assistente_late_dry_run", phone_last4=phone[-4:], chars=len(text))
        return

    try:
        EvolutionClient().send_text(
            instance_name=settings.evolution_assistente_instance,
            to_number=phone, text=text,
        )
    except Exception as e:  # noqa: BLE001
        log.error("assistente_late_send_error", phone_last4=phone[-4:], erro=str(e))


class AssistenteRunIn(BaseModel):
    from_phone: str = Field(...)
    text: str = Field(...)
    message_id: str | None = None
    channel: str | None = "whatsapp"


def _check_token(x_internal_token: str | None) -> None:
    expected = settings.copiloto_internal_token
    if not expected or x_internal_token != expected:
        raise HTTPException(status_code=401, detail="invalid token")


@router.post("/run")
async def assistente_run(
    payload: AssistenteRunIn,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> dict[str, Any]:
    _check_token(x_internal_token)

    try:
        phone = normalize_phone(payload.from_phone)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_phone")

    if payload.message_id and is_duplicate(payload.message_id):
        return {"status": "duplicate", "response_text": None}

    sender = identify_sender(phone)
    if sender.kind == "unknown":
        raise HTTPException(status_code=404, detail="not_mine")

    # Persist user msg
    try:
        append_message(
            phone=phone,
            sender_kind=sender.kind,
            sender_id=sender.id,
            message_role="user",
            text=payload.text,
            message_id=payload.message_id,
        )
    except Exception as e:
        # Race entre dedup check e insert (idx UNIQUE) — trate como duplicate
        if "idx_conversations_msgid" in str(e):
            return {"status": "duplicate", "response_text": None}
        raise

    history = load_history(phone)

    agent = AgenteAssistente(
        phone=phone,
        sender_kind=sender.kind,
        sender_id=sender.id,
        sender_nome=sender.nome,
        sender_papel=sender.papel,
        history=history,
    )

    guard = _DeliveryGuard()
    timed_out = threading.Event()
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(None, agent.run, payload.text)

    def _on_agent_done(f: "asyncio.Future[dict[str, Any]]") -> None:
        # Só age se o timeout HTTP já respondeu ao cliente. No caso rápido
        # (agent terminou antes do timeout) este callback vira no-op — quem
        # entrega é o corpo do endpoint (resposta no body, sem envio Evolution).
        if not timed_out.is_set():
            return
        if not guard.claim():
            return
        try:
            res = f.result()
        except Exception as e:  # noqa: BLE001
            log.error("assistente_late_agent_error", phone_last4=phone[-4:], erro=str(e))
            return
        _deliver_late_response(phone, res.get("response_text", ""))

    fut.add_done_callback(_on_agent_done)

    try:
        # shield: o timeout NÃO cancela o future — o agent segue rodando
        # e o callback acima entrega quando ficar pronto.
        result = await asyncio.wait_for(asyncio.shield(fut), timeout=AGENT_TIMEOUT_S)
    except asyncio.TimeoutError:
        # Marca timeout e reexecuta o callback caso o agent JÁ tenha terminado
        # nessa janela (a done-callback pode ter rodado como no-op).
        timed_out.set()
        if fut.done():
            _on_agent_done(fut)
        log.warning("assistente_run_timeout", phone=phone[-4:])
        return {
            "status": "timeout",
            "response_text": "Tô processando, te respondo em seguida. (Beti)",
            "sender": {"kind": sender.kind, "nome": sender.nome},
            "tool_calls": [],
        }

    # Terminou dentro do prazo: o caminho HTTP reivindica a entrega.
    # Se o callback correu primeiro (só possível pós-timeout), ele já entregou.
    if not guard.claim():
        return {
            "status": "ok",
            "response_text": result.get("response_text", ""),
            "sender": {"kind": sender.kind, "nome": sender.nome},
            "tool_calls": result.get("tool_calls", []),
        }

    response_text = result.get("response_text", "")
    tool_calls = result.get("tool_calls", [])

    # Persist assistant msg
    append_message(
        phone=phone,
        sender_kind="assistant",
        sender_id=None,
        message_role="assistant",
        text=response_text,
        message_id=None,
    )

    return {
        "status": "ok",
        "response_text": response_text,
        "sender": {"kind": sender.kind, "nome": sender.nome},
        "tool_calls": tool_calls,
    }


class BriefingTestIn(BaseModel):
    corretora_id: int = Field(...)
    destino_phone: str = Field(...)


@router.post("/briefing/test")
def assistente_briefing_test(
    payload: BriefingTestIn,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> dict[str, Any]:
    _check_token(x_internal_token)
    try:
        destino = normalize_phone(payload.destino_phone)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_phone")

    from app.db.models import Corretora
    from app.db.session import SessionLocal
    from sqlalchemy import select as _select

    with SessionLocal() as s:
        corretora = s.execute(
            _select(Corretora)
            .where(Corretora.id == payload.corretora_id)
            .where(Corretora.ativo.is_(True))
        ).scalar_one_or_none()
    if not corretora:
        raise HTTPException(status_code=404, detail="corretora_not_found_or_inactive")

    from app.services.briefing_corretora import enviar_briefing
    return enviar_briefing(corretora_id=payload.corretora_id, destino_phone=destino)


@router.get("/owns_phone")
def assistente_owns_phone(
    phone: str = Query(...),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> dict[str, Any]:
    """Quem é este telefone pro copiloto? Consumido pelo bridge da agencia.

    `owns` = corretora/dona (o bridge chama /assistente/run). `lead` = comprador
    conhecido nosso — NÃO é `owns` (a Assistente atende equipe, não lead), mas o
    bridge precisa saber pra não jogar na corretor, que é atendente da AGÊNCIA.

    Sem esse campo, lead que escreve direto pro número da Assistente ouve "aqui é a
    agencia, a gente não vende imóveis" — aconteceu com 80 leads entre 30/06 e
    02/08 (incidente da corretor, corrigido também no filtro de linha do bridge).
    """
    _check_token(x_internal_token)
    try:
        normalized = normalize_phone(phone)
    except ValueError:
        return {"owns": False}
    sender = identify_sender(normalized)
    if sender.kind == "unknown":
        lead = _lead_por_telefone(normalized)
        if lead is None:
            return {"owns": False}
        return {"owns": False, "lead": True, "nome": lead}
    return {"owns": True, "kind": sender.kind, "nome": sender.nome}


def _lead_por_telefone(phone_normalized: str) -> str | None:
    """Nome do lead com este telefone, ou None. Nunca levanta — o bridge não
    pode ficar sem roteamento porque o banco piscou."""
    from sqlalchemy import text as sql_text

    from app.db.session import SessionLocal

    so_digitos = phone_normalized.lstrip("+")
    try:
        with SessionLocal() as s:
            row = s.execute(
                sql_text(
                    "SELECT nome FROM leads "
                    "WHERE telefone = :p OR telefone = :p2 LIMIT 1"
                ),
                {"p": so_digitos, "p2": f"+{so_digitos}"},
            ).first()
    except Exception:  # noqa: BLE001
        log.warning("owns_phone_lead_lookup_falhou", exc_info=True)
        return None
    if row is None:
        return None
    return row[0] or "?"
