from typing import Any

import httpx
import structlog
from agno.tools import Toolkit
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import is_test_or_dryrun, settings
from app.db.session import SessionLocal
from app.db import models

log = structlog.get_logger()

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _is_server_error(exc: BaseException) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500


def _record_briefing(conteudo: str, success: bool, error_message: str | None = None) -> None:
    with SessionLocal() as session:
        row = models.BriefingSent(
            destinatario="dona",
            canal="telegram",
            conteudo=conteudo,
            success=success,
            is_test=is_test_or_dryrun(),
        )
        session.add(row)
        session.commit()
    if error_message:
        log.warning("telegram_send_failed", error=error_message)


class TelegramTool(Toolkit):
    """Envia mensagens para a Dona via Telegram Bot API."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(name="telegram_tool", tools=[self.send_to_dona], **kwargs)
        self.token: str = settings.telegram_bot_token
        self.chat_id: str = settings.telegram_dona_chat_id

    def send_to_dona(self, message: str) -> str:
        """Envia uma mensagem de texto para a Dona no Telegram.

        Args:
            message: Texto a enviar (suporta Markdown).

        Returns:
            'ok' se enviado com sucesso, mensagem de erro caso contrário.
        """
        if settings.dry_run:
            log.info("[DRY_RUN] Telegram", destinatario="dona", preview=message[:120])
            _record_briefing(conteudo=message, success=True)
            return "ok (dry_run)"

        try:
            result = self._post_with_retry(message)
            _record_briefing(conteudo=message, success=True)
            log.info("telegram_sent", chat_id=self.chat_id, msg_id=result.get("message_id"))
            return "ok"
        except Exception as exc:
            error_msg = str(exc)
            _record_briefing(conteudo=message, success=False, error_message=error_msg)
            return f"error: {error_msg}"

    @retry(
        retry=retry_if_exception(_is_server_error),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    def _post_with_retry(self, message: str) -> dict[str, Any]:
        url = _TELEGRAM_API.format(token=self.token)
        with httpx.Client(timeout=10) as client:
            resp = client.post(url, json={
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "Markdown",
            })
            resp.raise_for_status()
            return resp.json().get("result", {})


# ── Pessoa Pessoa9 via bot corretor (Telegram) ─────────────────────────────────────
# Decisão da Dona 01/07/2026: a cópia do resumo das 20h pra Pessoa sai do WhatsApp
# (2955 nunca inicia conversa) e vai pro Telegram dela, pelo bot corretor que já roda
# na bridge (/opt/telegram-claude/corretor.env). sendMessage não conflita com o
# polling da bridge (getUpdates é que conflitaria — nunca usar aqui).

_CORRETOR_ENV = "/opt/telegram-claude/corretor.env"


def _corretor_config() -> tuple[str, str] | None:
    """(token, chat_id da Pessoa) do corretor.env, ou None se indisponível."""
    try:
        token, chats = "", ""
        with open(_CORRETOR_ENV) as f:
            for line in f:
                line = line.strip()
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("ALLOWED_CHAT_IDS="):
                    chats = line.split("=", 1)[1].strip().strip('"').strip("'")
        chat_id = chats.split(",")[0].strip() if chats else ""
        if token and chat_id:
            return token, chat_id
    except OSError as exc:
        log.warning("corretor_env_indisponivel", erro=str(exc)[:80])
    return None


def send_to_pessoa_telegram(message: str) -> bool:
    """Envia o briefing pra Pessoa via bot corretor. Grava em briefings_sent."""
    import os
    # GUARDA ANTI-TESTE (incidentes 01/07: executor E revisora mandaram msg real
    # pra Pessoa rodando pytest). pytest seta PYTEST_CURRENT_TEST — sob teste,
    # NUNCA envia de verdade, independente de mock esquecido.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        log.info("pessoa_telegram_bloqueado_pytest", preview=message[:60])
        return True

    def _record(success: bool) -> None:
        with SessionLocal() as session:
            session.add(models.BriefingSent(
                destinatario="pessoa", canal="telegram",
                conteudo=message, success=success,
                is_test=is_test_or_dryrun(),
            ))
            session.commit()

    if settings.dry_run:
        log.info("[DRY_RUN] Telegram", destinatario="pessoa", preview=message[:120])
        _record(True)
        return True

    cfg = _corretor_config()
    if cfg is None:
        log.warning("pessoa_telegram_sem_config")
        return False
    token, chat_id = cfg
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                _TELEGRAM_API.format(token=token),
                json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            )
            if resp.status_code == 400:
                # Markdown inválido derruba o sendMessage — retenta texto puro.
                resp = client.post(
                    _TELEGRAM_API.format(token=token),
                    json={"chat_id": chat_id, "text": message},
                )
            resp.raise_for_status()
        _record(True)
        log.info("telegram_sent", destinatario="pessoa")
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort, caller notifica
        _record(False)
        log.warning("pessoa_telegram_falhou", erro=str(exc)[:120])
        return False
