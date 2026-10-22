"""Webhook receiver pro Evolution (chip Acme = instância 'assistente')."""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Body, Path, Response

from app.config import settings
from app.integrations.evolution_client import EvolutionClient
from app.services.conversations import append_message, is_duplicate
from app.services.assistente_silence import is_silenced as _assistente_silenced
from app.services.mensagens_evolution_service import gravar_mensagem_evolution
from app.services.phone import normalize_phone
from app.services.sender_lookup import identify_sender
from app.services.hunter_forward import forward_to_hunter
from app.recovery import repo as recovery_repo
from app.recovery.orchestrator import process_incoming as recovery_process
from app.db.session import SessionLocal

log = structlog.get_logger()

router = APIRouter(prefix="/webhook/evolution", tags=["webhook"])

_AUDIO_MSG_KEYS = ("audioMessage", "pttMessage")


def _is_audio_payload(payload: dict[str, Any]) -> bool:
    msg = (payload.get("data") or {}).get("message") or {}
    return any(k in msg for k in _AUDIO_MSG_KEYS)


async def _transcrever_async(mensagem_id: uuid.UUID) -> None:
    from app.services.transcricao_service import transcrever_mensagem
    try:
        await asyncio.to_thread(transcrever_mensagem, mensagem_id)
    except Exception as exc:
        log.error("transcricao_async_inesperado", mensagem_id=str(mensagem_id), erro=str(exc))


def _extract_message(payload: dict[str, Any]) -> dict[str, Any] | None:
    """
    Evolution v2 manda payload tipo:
      {event: "messages.upsert", data: {key: {remoteJid, id, fromMe},
                                        message: {conversation | extendedTextMessage:{text} |
                                                  templateButtonReplyMessage:{selectedId} |
                                                  imageMessage:{caption} | audioMessage | ...}}}

    Retorna também `kind` pra caller decidir fluxo:
      - "text"        — texto puro do lead
      - "button"      — clique em botão de HSM (selectedId vira o texto)
      - "media"       — áudio/imagem/sticker/etc (text pode estar vazio ou ser caption)
    """
    event = payload.get("event")
    if event != "messages.upsert":
        return None
    data = payload.get("data") or {}
    key = data.get("key") or {}
    # NÃO descarta fromMe=True aqui — corretora mandando pra Assistente vem
    # como fromMe=true na instância da própria corretora. O filtro real
    # acontece embaixo via dest_phone != assistente_phone (linha ~185).

    remote = key.get("remoteJid") or ""
    if "@" not in remote:
        return None
    raw_phone = remote.split("@", 1)[0]

    msg = data.get("message") or {}
    message_type = data.get("messageType") or ""

    # 1. Texto direto
    text = msg.get("conversation")
    kind = "text" if text else None

    # 2. Texto estendido (citações, links)
    if not text:
        ext = msg.get("extendedTextMessage") or {}
        text = ext.get("text")
        if text:
            kind = "text"

    # 3. Clique em botão de HSM ou lista
    if not text:
        btn = msg.get("templateButtonReplyMessage") or {}
        text = btn.get("selectedId") or btn.get("selectedDisplayText")
        if text:
            kind = "button"
    if not text:
        btn = msg.get("buttonsResponseMessage") or {}
        text = btn.get("selectedDisplayText") or btn.get("selectedButtonId")
        if text:
            kind = "button"
    if not text:
        lst = msg.get("listResponseMessage") or {}
        text = lst.get("title") or (lst.get("singleSelectReply") or {}).get("selectedRowId")
        if text:
            kind = "button"

    # 4. Caption de imagem/vídeo/documento
    if not text:
        for k in ("imageMessage", "videoMessage", "documentMessage", "documentWithCaptionMessage"):
            sub = msg.get(k) or {}
            cap = sub.get("caption")
            if cap:
                text = cap
                kind = "media"
                break

    # 5. Áudio/sticker/contato/reação — sem texto extraível, mas vale registrar tipo
    if not text:
        for k in ("audioMessage", "pttMessage"):
            if k in msg:
                text = "[áudio recebido]"
                kind = "media"
                break
    if not text and "stickerMessage" in msg:
        text = "[sticker]"
        kind = "media"
    if not text and "contactMessage" in msg:
        contact = msg.get("contactMessage") or {}
        text = f"[contato compartilhado: {contact.get('displayName') or 'sem nome'}]"
        kind = "media"
    if not text and "reactionMessage" in msg:
        reac = msg.get("reactionMessage") or {}
        text = f"[reação: {reac.get('text') or '?'}]"
        kind = "media"

    if not text:
        return None

    return {
        "raw_phone": raw_phone,
        "text": text,
        "message_id": key.get("id"),
        "kind": kind or "text",
        "message_type": message_type,
    }


@router.post("/assistente/{secret}")
async def evolution_webhook_assistente(
    secret: str = Path(...),
    payload: dict[str, Any] = Body(...),
) -> Response:
    if secret != settings.evolution_webhook_secret or not settings.evolution_webhook_secret:
        return Response(status_code=404)

    # Auditoria — instance vem do payload (Evolution v2: {"instance": "acme_corretora_a", ...})
    # settings.evolution_assistente_instance serve apenas pro routing Assistente (send_text), nunca pra atribuição
    instance_name: str | None = payload.get("instance") or None
    if not instance_name:
        log.error("webhook_payload_sem_instance", payload_keys=list(payload.keys()))
    else:
        inserted_id = gravar_mensagem_evolution(instance=instance_name, payload=payload)
        # Áudio que NÃO vai pra Assistente: transcreve em background (auditoria)
        # Áudio que VAI pra Assistente: transcreve síncrono mais abaixo, antes de invocar agente.
        _is_audio = _is_audio_payload(payload)
        _will_invoke_assistente = (instance_name == settings.evolution_assistente_instance)
        if inserted_id is not None and _is_audio and not _will_invoke_assistente:
            asyncio.create_task(_transcrever_async(inserted_id))

    extracted = _extract_message(payload)
    if not extracted:
        log.info("webhook_skip_non_message", evt_type=payload.get("event"))
        return Response(status_code=200)

    # ── Routing por DESTINATÁRIO ──────────────────────────────────────────────
    # A instância `assistente` da Assistente não está disparando webhook hoje (a investigar
    # separado). As mensagens chegam via instância da CORRETORA remetente (fromMe=true).
    # Filtro correto: a msg só dispara Assistente se for destinada AO telefone dela.
    #   fromMe=true  → dest = remoteJid (pra quem mandaram)
    #   fromMe=false → dest = owner da instância (quem recebeu)
    # Sem isso, qualquer msg corretora→Dona/outra-corretora disparava Assistente
    # respondendo a quem nunca a endereçou (incidente 2026-05-17, ~140 disparos espúrios).
    key = (payload.get("data") or {}).get("key") or payload.get("key") or {}
    from_me = bool(key.get("fromMe"))
    if from_me:
        try:
            dest_phone = normalize_phone(extracted["raw_phone"])
        except ValueError:
            return Response(status_code=200)
    else:
        # Mensagem chegando: só é pra Assistente se a instância for a dela
        if instance_name != settings.evolution_assistente_instance:
            return Response(status_code=200)
        dest_phone = settings.assistente_phone

    if dest_phone != settings.assistente_phone:
        return Response(status_code=200)

    try:
        phone = normalize_phone(extracted["raw_phone"])
    except ValueError:
        log.warning("webhook_bad_phone", raw=extracted["raw_phone"][-6:])
        return Response(status_code=200)

    if extracted["message_id"] and is_duplicate(extracted["message_id"]):
        log.info("webhook_duplicate", msg_id=extracted["message_id"])
        return Response(status_code=200)

    # Silencia Assistente quando a Assistente (Claude/OpenClaw) tá tocando a conversa.
    # Setado manualmente via scripts/assistente_silence.py <phone> [minutes].
    if _assistente_silenced(phone):
        log.info("webhook_assistente_silenced", phone_last4=phone[-4:])
        return Response(status_code=200)

    # ── Porteiro: corretora/dona conhecida fica na Assistente; resto → Hunter ──
    # O copiloto não conhece mais o banco do Hunter. Se o sender não é
    # corretora/dona cadastrada, encaminha pro serviço do Hunter (:8011), que
    # decide o que fazer. Fire-and-forget em thread pra não travar o event loop
    # (o Hunter faz LLM pesado e manda a resposta sozinho via Evolution).
    sender = identify_sender(phone)
    if sender.kind == "unknown":
        if not from_me:
            asyncio.create_task(
                asyncio.to_thread(
                    forward_to_hunter, phone, extracted["text"], extracted["message_id"]
                )
            )
        log.info("webhook_forwarded_or_unknown", phone_last4=phone[-4:])
        return Response(status_code=200)

    try:
        append_message(
            phone=phone,
            sender_kind=sender.kind,
            sender_id=sender.id,
            message_role="user",
            text=extracted["text"],
            message_id=extracted["message_id"],
        )
    except Exception as e:
        if "idx_conversations_msgid" in str(e):
            return Response(status_code=200)
        log.error("webhook_persist_error", error=str(e))
        return Response(status_code=200)

    # Áudio pra Assistente: transcreve SÍNCRONO antes de invocar agente,
    # senão Assistente recebe "[áudio recebido]" literal e responde "não entendi".
    if _is_audio and inserted_id is not None:
        try:
            from app.services.transcricao_service import transcrever_mensagem
            from app.db.models import MensagemWhatsapp
            from sqlalchemy import select
            from app.db.session import SessionLocal
            ok = transcrever_mensagem(inserted_id)
            if ok:
                with SessionLocal() as s:
                    msg_row = s.execute(
                        select(MensagemWhatsapp).where(MensagemWhatsapp.id == inserted_id)
                    ).scalar_one_or_none()
                    if msg_row and msg_row.transcricao:
                        extracted["text"] = "🎤 " + msg_row.transcricao.strip()
                        log.info("webhook_audio_transcrito_inline",
                                 phone_last4=phone[-4:],
                                 texto_len=len(msg_row.transcricao))
        except Exception as e:
            log.error("webhook_audio_transcribe_inline_falhou",
                      phone_last4=phone[-4:], erro=str(e))

    # Enfileira pro dispatcher (debounce + typing + agent run + send).
    # Handler retorna 200 imediato pro Evolution.
    from app.services.assistente_dispatch import enqueue_assistente_run
    enqueue_assistente_run(
        phone=phone,
        text=extracted["text"],
        sender_kind=sender.kind,
        sender_id=sender.id,
        sender_nome=sender.nome,
        sender_papel=sender.papel,
    )
    return Response(status_code=200)


# ── Recuperador (Corretor) ────────────────────────────────────────────────────────

@router.post("/recuperador/{secret}")
def evolution_webhook_recuperador(
    secret: str = Path(...),
    payload: dict[str, Any] = Body(...),
) -> Response:
    """
    Webhook do número de reativação (instância `recuperador`).

    Só processa mensagens vindas de leads (fromMe=false) na instância recuperador.
    Lookup do lead_id em recovery_conversations pelo telefone — a conversa
    deve ter sido pré-criada pelo dispatcher ao disparar o HSM.

    Endpoint é `def` (não async) porque o orchestrator chama `claude -p` via
    subprocess síncrono — FastAPI roda funções sync em threadpool automaticamente,
    evitando travar o event loop (CLAUDE.md anti-pattern conhecido).
    """
    if secret != settings.evolution_webhook_secret or not settings.evolution_webhook_secret:
        return Response(status_code=404)

    instance_name = payload.get("instance") or None
    if instance_name != settings.evolution_recuperador_instance:
        # Não é a instância recuperador — ignora silenciosamente
        return Response(status_code=200)

    # Auditoria (mesmo padrão do assistente webhook) — captura o retorno pra dedup.
    inserted_id = None
    if instance_name:
        inserted_id = gravar_mensagem_evolution(instance=instance_name, payload=payload)

    extracted = _extract_message(payload)
    if not extracted:
        return Response(status_code=200)

    try:
        phone = normalize_phone(extracted["raw_phone"])
    except ValueError:
        log.warning("recuperador_bad_phone", raw=extracted["raw_phone"][-6:])
        return Response(status_code=200)

    # Dedup CORRETO (Codex #5): a gravação em mensagens_whatsapp é idempotente
    # (ON CONFLICT DO NOTHING) e só retorna UUID quando a mensagem é NOVA — None
    # em reentrega. Antes usava is_duplicate(), que consulta a tabela
    # `conversations`, onde o recovery NUNCA grava (usa recovery_messages) →
    # nunca deduplicava e a Corretor respondia de novo a cada reentrega do webhook.
    if extracted["message_id"] and inserted_id is None:
        log.info("recuperador_msg_duplicada", message_id=extracted["message_id"])
        return Response(status_code=200)

    # Lookup do lead na recovery_conversations (pré-criado pelo dispatcher HSM)
    with SessionLocal() as session:
        conv = recovery_repo.find_by_phone(session, phone)

    if conv is None:
        log.info(
            "recuperador_unknown_phone",
            phone_last4=phone[-4:],
            text_preview=extracted["text"][:60],
        )
        return Response(status_code=200)

    # Atalho: clique no botão de sair = opt-out automático.
    # Não chama Corretor, marca perdido/opt_out, move pro status "Venda perdida".
    #
    # A comparação era LITERAL com "PARAR DE RECEBER" — o texto do botão antigo.
    # Os templates novos trazem "Parar mensagens", então cinco pessoas clicaram
    # entre 06 e 19/08/2026 (Fulano, Fulano, Fulano, Fulano, Fulano) e nada
    # foi gravado: seguiram elegíveis pro próximo disparo. Agora quem decide é
    # `is_opt_out`, o mesmo detector do texto livre — botão novo, botão velho ou
    # o que vier depois entram todos pela mesma porta.
    from app.services.opt_out import is_opt_out as _quer_parar

    if extracted.get("kind") == "button" and _quer_parar(extracted["text"]):
        from app.recovery.conversation import State
        from app.tools.kommo import KommoTool
        try:
            with SessionLocal() as session:
                row = recovery_repo.find_by_phone(session, phone)
                if row and row.state not in (State.PERDIDO.value, State.LARGADO.value):
                    row.state = State.PERDIDO.value
                    row.end_reason = "opt_out"
                    recovery_repo.append_message(
                        session, row.lead_id, role="lead", text="PARAR DE RECEBER",
                    )
                    session.commit()
                    try:
                        kommo = KommoTool()
                        kommo.move_lead_stage(
                            row.lead_id,
                            int(settings.kommo_stage_remarketing_perdida),
                            pipeline_id=settings.kommo_pipeline_id_remarketing or None,
                        )
                        kommo.add_lead_note(
                            row.lead_id,
                            'Cliente clicou em "PARAR DE RECEBER" no HSM — opt-out automático',
                        )
                    except Exception as ke:
                        log.error("recuperador_optout_kommo_failed", lead_id=row.lead_id, error=str(ke))
            log.info("recuperador_optout_processed", phone_last4=phone[-4:])
        except Exception as e:
            log.error("recuperador_optout_failed", phone_last4=phone[-4:], error=str(e))
        return Response(status_code=200)

    # Filtra auto-resposta de WhatsApp Business (não conta como conversa real)
    from app.recovery.autoreply import is_autoreply
    seconds_since_hsm: float | None = None
    if conv.hsm_sent_at is not None:
        from datetime import datetime, timezone
        seconds_since_hsm = (
            datetime.now(timezone.utc) - conv.hsm_sent_at
        ).total_seconds()
    auto, hits = is_autoreply(extracted["text"], seconds_since_hsm)
    if auto:
        log.info(
            "recuperador_autoreply_skipped",
            lead_id=conv.lead_id,
            seconds_since_hsm=seconds_since_hsm,
            pattern_hits=hits,
            text_preview=extracted["text"][:80],
        )
        return Response(status_code=200)

    try:
        result = recovery_process(
            lead_phone=phone,
            lead_name=conv.lead_name,
            lead_id=conv.lead_id,
            text=extracted["text"],
        )
        log.info(
            "recuperador_processed",
            lead_id=conv.lead_id,
            sent=result.sent,
            state=result.state,
            skipped=result.skipped_reason,
        )
    except Exception as e:
        log.error("recuperador_orchestrator_error", lead_id=conv.lead_id, error=str(e))

    return Response(status_code=200)
