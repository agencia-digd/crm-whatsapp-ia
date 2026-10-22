"""Webhook do Telegram → AgenteAssistente.

Recebe Updates do bot @copiloto_acme_bot e responde via Assistente.
Tudo bloqueado a `TELEGRAM_DONA_CHAT_ID` — só a Dona conversa.

Sessão persistente: usa tabela `telegram_chat_log` pra carregar últimas
20 mensagens da conversa como history pro agente — Assistente lembra do
contexto entre chamadas.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text as sql_text

from app.agents.assistente.agent import AgenteAssistente
from app.config import settings
from app.db.session import SessionLocal

log = structlog.get_logger()

router = APIRouter(prefix="/api/telegram", tags=["telegram"])

_TG_API = "https://api.telegram.org/bot{token}/sendMessage"
_TG_GET_FILE = "https://api.telegram.org/bot{token}/getFile"
_TG_FILE_DL = "https://api.telegram.org/file/bot{token}/{path}"
_HISTORY_LIMIT = 20


def _telegram_baixar_audio(file_id: str) -> tuple[bytes | None, str]:
    """Baixa arquivo de áudio do Telegram. Retorna (bytes, mime_type)."""
    try:
        with httpx.Client(timeout=30.0) as cli:
            r1 = cli.get(_TG_GET_FILE.format(token=settings.telegram_bot_token),
                         params={"file_id": file_id})
            if r1.status_code != 200:
                log.warning("telegram_getfile_error", status=r1.status_code)
                return None, ""
            file_path = (r1.json().get("result") or {}).get("file_path")
            if not file_path:
                return None, ""
            r2 = cli.get(_TG_FILE_DL.format(token=settings.telegram_bot_token, path=file_path))
            if r2.status_code != 200:
                return None, ""
            # Telegram voice = OGG opus; audio = vários formatos
            mime = "audio/ogg" if file_path.endswith(".oga") or file_path.endswith(".ogg") else "audio/mpeg"
            return r2.content, mime
    except Exception as e:
        log.error("telegram_audio_download_error", error=str(e))
        return None, ""


def _telegram_send(chat_id: int | str, text: str) -> None:
    """Envia mensagem via Telegram Bot API. Suporta texto longo (split em 4096)."""
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]
    url = _TG_API.format(token=settings.telegram_bot_token)
    try:
        with httpx.Client(timeout=15.0) as cli:
            for chunk in chunks:
                r = cli.post(url, json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                })
                if r.status_code >= 400:
                    log.warning("telegram_send_error", status=r.status_code, body=r.text[:200])
                    # tenta sem parse_mode (texto pode quebrar markdown)
                    cli.post(url, json={"chat_id": chat_id, "text": chunk})
    except Exception as e:
        log.error("telegram_send_exception", error=str(e))


def _claim_update(update_id: int | None, chat_id: int | None,
                  message_id: int | None) -> bool:
    """Claim transacional idempotente de um update do Telegram (FURO 3).

    Insere update_id em telegram_updates_processed com ON CONFLICT DO NOTHING.
    Retorna True se ESTE processo ganhou o claim (deve seguir), False se o
    update já foi processado/reivindicado (é duplicata — descartar).

    Sem update_id (não deveria acontecer em Updates reais) → deixa passar, mas
    não dá pra deduplicar; loga o caso. Erro de banco → fail-OPEN aqui de
    propósito: preferimos processar (possivelmente 2x, raro) a derrubar o
    atendimento da Dona se a tabela de dedup estiver indisponível.
    """
    if update_id is None:
        log.warning("telegram_update_sem_update_id")
        return True
    try:
        with SessionLocal() as s:
            res = s.execute(
                sql_text(
                    """INSERT INTO telegram_updates_processed
                           (update_id, chat_id, message_id)
                       VALUES (:uid, :cid, :mid)
                       ON CONFLICT (update_id) DO NOTHING
                       RETURNING update_id"""
                ),
                {"uid": update_id, "cid": chat_id, "mid": message_id},
            ).first()
            s.commit()
        claimed = res is not None
        if not claimed:
            log.info("telegram_update_duplicado_descartado", update_id=update_id)
        return claimed
    except Exception as e:
        log.error("telegram_claim_update_error", update_id=update_id, error=str(e))
        return True


def _load_history(chat_id: int) -> list[dict[str, Any]]:
    """Carrega últimas N mensagens da conversa, ordem cronológica (antiga → nova)."""
    with SessionLocal() as s:
        rows = s.execute(
            sql_text(
                """SELECT role, text, created_at
                   FROM telegram_chat_log
                   WHERE chat_id = :cid
                   ORDER BY created_at DESC
                   LIMIT :lim"""
            ),
            {"cid": chat_id, "lim": _HISTORY_LIMIT},
        ).all()
    # inverte pra ordem cronológica
    return [
        {"role": r[0], "text": r[1], "created_at": r[2]}
        for r in reversed(rows)
    ]


def _log_message(chat_id: int, role: str, text: str,
                 message_id: int | None = None,
                 tool_calls: list[dict[str, Any]] | None = None) -> None:
    import json as _json
    with SessionLocal() as s:
        s.execute(sql_text(
            """INSERT INTO telegram_chat_log (chat_id, role, text, message_id, tool_calls)
               VALUES (:cid, :role, :text, :mid, CAST(:tc AS jsonb))"""
        ), {
            "cid": chat_id, "role": role, "text": text,
            "mid": message_id,
            "tc": _json.dumps(tool_calls) if tool_calls else None,
        })
        s.commit()


@router.post("/webhook/{secret}")
async def telegram_webhook(secret: str, req: Request) -> dict[str, Any]:
    if not settings.telegram_webhook_secret or secret != settings.telegram_webhook_secret:
        raise HTTPException(403, "secret inválido")

    payload = await req.json()
    log.info("telegram_update_received", update_id=payload.get("update_id"))

    msg = payload.get("message") or payload.get("edited_message")
    if not msg:
        return {"status": "skipped", "reason": "no_message"}

    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return {"status": "skipped", "reason": "no_chat_id"}

    # Lock por chat — só a Dona fala com o bot
    allowed_chat = settings.telegram_dona_chat_id
    if str(chat_id) != str(allowed_chat):
        log.warning("telegram_chat_blocked", chat_id=chat_id, user=msg.get("from", {}).get("username"))
        # silencioso — não responde
        return {"status": "ignored", "reason": "chat_not_allowed"}

    # FURO 3: dedup/serialização por update_id. Claim transacional ANTES de
    # qualquer efeito (transcrição, resposta, tools mutáveis). Reentrega do
    # Telegram ou 2 requisições concorrentes → só a 1ª ganha o claim; a
    # duplicata é descartada aqui sem re-executar o agente.
    update_id = payload.get("update_id")
    if not _claim_update(update_id, chat_id, msg.get("message_id")):
        return {"status": "duplicate", "reason": "update_already_processed",
                "update_id": update_id}

    text = (msg.get("text") or "").strip()
    audio_part = msg.get("voice") or msg.get("audio")
    transcrito = False

    if not text and audio_part:
        # Áudio: download + transcreve via Gemini
        file_id = audio_part.get("file_id")
        _telegram_send(chat_id, "🎙️ ouvi seu áudio, transcrevendo…")
        audio_bytes, mime = _telegram_baixar_audio(file_id) if file_id else (None, "")
        if not audio_bytes:
            _telegram_send(chat_id, "Não consegui baixar o áudio. Manda em texto?")
            return {"status": "error", "reason": "audio_download_failed"}
        try:
            from app.services.transcricao_service import transcrever_audio
            t = transcrever_audio(audio_bytes, mime)
            if t.get("erro") or not t.get("texto"):
                _telegram_send(chat_id, f"Não consegui transcrever ({t.get('erro') or 'vazio'}). Manda em texto?")
                return {"status": "error", "reason": "transcribe_failed"}
            text = t["texto"].strip()
            transcrito = True
            _telegram_send(chat_id, f"📝 *Entendi assim:* \"{text}\"\n\nProcessando…")
        except Exception as e:
            log.error("telegram_transcribe_exception", error=str(e))
            _telegram_send(chat_id, "Tive problema na transcrição. Manda em texto?")
            return {"status": "error", "reason": "transcribe_exception"}

    if not text:
        if msg.get("photo"):
            _telegram_send(chat_id, "🖼️ Recebi a foto! Análise visual tá em desenvolvimento — me descreva em texto o que tu queria que eu olhasse?")
        else:
            _telegram_send(chat_id, "Não entendi o tipo da mensagem. Manda em texto que respondo na hora.")
        return {"status": "ok", "reason": "non_text"}

    message_id = msg.get("message_id")

    # Comandos especiais
    if text.startswith("/start"):
        _telegram_send(chat_id, (
            "🐱 *Oi Dona!* Sou eu, a Assistente.\n\n"
            "Agora a gente conversa por aqui também — me chama quando precisar.\n\n"
            "Eu posso:\n"
            "• Registrar visita/venda/ligação\n"
            "• Resumir o funil do mês\n"
            "• Mandar mensagem pras corretoras (com sua confirmação)\n"
            "• Adicionar notas\n\n"
            "Manda áudio/foto e eu te aviso o que ainda não consigo processar."
        ))
        _log_message(chat_id, "user", text, message_id=message_id)
        _log_message(chat_id, "assistant", "[boas vindas]")
        return {"status": "ok"}

    if text.startswith("/reset"):
        with SessionLocal() as s:
            s.execute(sql_text("DELETE FROM telegram_chat_log WHERE chat_id = :cid"), {"cid": chat_id})
            s.commit()
        _telegram_send(chat_id, "Memória zerada. Começamos do zero! 🐱")
        return {"status": "ok"}

    # Log da pergunta
    _log_message(chat_id, "user", text, message_id=message_id)

    # Carrega history e roda Assistente em threadpool (subprocess CLI é blocking;
    # não pode rodar no event loop senão trava o uvicorn inteiro).
    from starlette.concurrency import run_in_threadpool
    history = _load_history(chat_id)

    def _run_agent() -> dict[str, Any]:
        agent = AgenteAssistente(
            sender_kind="dona",
            sender_nome="Dona",
            history=history,
        )
        return agent.run(text)

    try:
        result = await run_in_threadpool(_run_agent)
        response = result.get("response_text", "Tive um problema, tenta de novo?")
        tool_calls = result.get("tool_calls") or []
    except Exception as e:
        log.error("telegram_agent_error", error=str(e))
        response = "Tive um problema técnico aqui. Tenta de novo daqui a pouco."
        tool_calls = []

    _telegram_send(chat_id, response)
    _log_message(chat_id, "assistant", response, tool_calls=tool_calls)

    return {
        "status": "ok",
        "response_preview": response[:80],
        "tool_calls": len(tool_calls),
    }
