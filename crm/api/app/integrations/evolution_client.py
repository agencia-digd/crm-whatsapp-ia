"""
Cliente HTTP para Evolution API v2.

Padrões:
- apikey via header (nunca em query string)
- POST /chat/findChats e POST /chat/findMessages para leitura
- Retry 3x exponencial apenas em 5xx; 4xx não retentar
- Timeout 30s por chamada
- Logs estruturados; conteúdo de mensagem NUNCA vai para log
"""
import os
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import settings

log = structlog.get_logger()


def _blocked_under_pytest() -> bool:
    """Trava dura contra envio REAL de WhatsApp durante testes.

    `COPILOTO_UNDER_PYTEST` é setado no topo de tests/conftest.py e vale pra
    sessão inteira do pytest (nunca é limpo) — resiste inclusive a entregas
    tardias em background que disparam DEPOIS do teste terminar (ver o caminho
    _deliver_late_response em app/api/assistente.py). Em produção a env nunca
    existe, então o envio segue normal.

    Caso de referência — 04/05/2026 e 11/07/2026: baterias de teste rodadas
    contra o ambiente de produção (DRY_RUN=false) vazaram mensagens mock
    ("DRYRUN_FORMAT_CHECK", "tarde demais") pro WhatsApp real de corretora/dona.
    """
    return os.environ.get("COPILOTO_UNDER_PYTEST") == "1"


def _is_server_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, (httpx.TimeoutException, httpx.ConnectError))


class EvolutionClient:
    """
    Cliente read-only para Evolution API v2.

    Args:
        base_url: URL base da API (ex: http://localhost:8090)
        api_key: apikey de autenticação
        timeout: timeout em segundos (default 30)
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: int = 30,
    ) -> None:
        self.base_url = (base_url or settings.evolution_api_url).rstrip("/")
        self.timeout = timeout
        self._headers = {
            "apikey": api_key or settings.evolution_api_key,
            "Content-Type": "application/json",
        }

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    @retry(
        retry=retry_if_exception(_is_server_error),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=15),
        reraise=True,
    )
    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self.base_url + path
        log.debug("evolution_request", method="GET", path=path)
        with httpx.Client(headers=self._headers, timeout=self.timeout) as client:
            resp = client.get(url, params=params)
            log.debug("evolution_response", status=resp.status_code, path=path)
            resp.raise_for_status()
            return resp.json()

    @retry(
        retry=retry_if_exception(_is_server_error),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=15),
        reraise=True,
    )
    def _post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        url = self.base_url + path
        log.debug("evolution_request", method="POST", path=path)
        with httpx.Client(headers=self._headers, timeout=self.timeout) as client:
            resp = client.post(url, json=body or {})
            log.debug("evolution_response", status=resp.status_code, path=path)
            resp.raise_for_status()
            return resp.json()

    # ── Public methods ────────────────────────────────────────────────────────

    def fetch_instances(self) -> list[dict[str, Any]]:
        """Lista todas as instâncias e seus estados."""
        result = self._get("/instance/fetchInstances")
        return result if isinstance(result, list) else []

    def get_instance_status(self, instance_name: str) -> str:
        """
        Retorna estado de conexão da instância.

        Returns:
            'open' | 'connecting' | 'close' | 'unknown'
        """
        try:
            data = self._get(f"/instance/connectionState/{instance_name}")
            return str(data.get("instance", {}).get("state", "unknown"))
        except httpx.HTTPStatusError as exc:
            log.error("evolution_error", path=f"connectionState/{instance_name}",
                      status=exc.response.status_code)
            return "unknown"

    def find_chats(
        self,
        instance_name: str,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """
        Lista chats da instância.

        Args:
            instance_name: nome da instância Evolution
            since: filtrar chats atualizados após este datetime (UTC)

        Returns:
            Lista de dicts com remoteJid, pushName, updatedAt, unreadCount, lastMessage
        """
        data = self._post(f"/chat/findChats/{instance_name}")
        chats: list[dict[str, Any]] = data if isinstance(data, list) else []

        if since:
            since_aware = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
            filtered = []
            for chat in chats:
                upd = chat.get("updatedAt") or ""
                if not upd:
                    continue
                try:
                    dt = datetime.fromisoformat(upd.replace("Z", "+00:00"))
                    if dt >= since_aware:
                        filtered.append(chat)
                except ValueError:
                    pass
            return filtered

        return chats

    def find_messages(
        self,
        instance_name: str,
        remote_jid: str,
        limit: int = 50,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """
        Busca mensagens de uma conversa.

        Args:
            instance_name: nome da instância Evolution
            remote_jid: JID do contato (ex: "5500000000000...@s.whatsapp.net")
            limit: máximo de mensagens por página
            page: número da página (1-indexed)

        Returns:
            Lista de mensagens (dicts brutos da API — normalização na EvolutionTool)
        """
        body: dict[str, Any] = {
            "where": {"key": {"remoteJid": remote_jid}},
            "limit": limit,
            "page": page,
        }
        data = self._post(f"/chat/findMessages/{instance_name}", body)
        # Resposta: {"messages": {"total": N, "pages": N, "records": [...]}}
        records: list[dict[str, Any]] = (
            data.get("messages", {}).get("records", [])
            if isinstance(data, dict)
            else []
        )
        log.info(
            "evolution_messages_fetched",
            instance=instance_name,
            jid_suffix=remote_jid[-10:],
            count=len(records),
        )
        return records

    def send_presence(
        self,
        instance_name: str,
        to_number: str,
        presence: str = "composing",
        delay_ms: int = 3000,
    ) -> dict[str, Any]:
        """
        Envia presença ("composing" = digitando, "recording" = gravando, "paused").

        Best-effort: erros são logados mas não levantados, pra nunca bloquear
        o fluxo principal de resposta da Assistente.
        """
        try:
            body = {"number": to_number, "presence": presence, "delay": delay_ms}
            return self._post(f"/chat/sendPresence/{instance_name}", body)  # type: ignore[return-value]
        except Exception as e:
            log.warning("evolution_send_presence_falhou",
                        instance=instance_name, to_last4=to_number[-4:],
                        erro=str(e)[:120])
            return {"presence": "error"}

    def send_text(
        self,
        instance_name: str,
        to_number: str,
        text: str,
    ) -> dict[str, Any]:
        """
        Envia mensagem de texto via Evolution API.

        Args:
            instance_name: nome da instância Evolution
            to_number: número de destino em dígitos (ex: "+5500000000000")
            text: conteúdo da mensagem

        Returns:
            Dict com campos key, status, messageTimestamp da API

        Raises:
            httpx.HTTPStatusError: em erros 4xx/5xx
        """
        # Nunca logar o conteúdo de text — só metadados
        log.info(
            "evolution_send_text",
            instance=instance_name,
            to_last4=to_number[-4:] if len(to_number) >= 4 else "??",
            length_chars=len(text),
        )
        if _blocked_under_pytest():
            log.warning(
                "evolution_send_text_bloqueado_pytest",
                instance=instance_name,
                to_last4=to_number[-4:] if len(to_number) >= 4 else "??",
                length_chars=len(text),
            )
            return {"status": "BLOCKED_UNDER_PYTEST", "key": None}
        body: dict[str, Any] = {"number": to_number, "text": text}
        return self._post(f"/message/sendText/{instance_name}", body)  # type: ignore[return-value]

    def get_base64_media(
        self,
        instance_name: str,
        message_id: str,
    ) -> dict[str, Any]:
        """
        Baixa a mídia (áudio/imagem/etc) de uma mensagem como base64.

        Args:
            instance_name: nome da instância Evolution
            message_id: key.id da mensagem de mídia

        Returns:
            Dict com 'base64', 'mimetype', 'mediaType', 'fileName'.
        """
        body: dict[str, Any] = {"message": {"key": {"id": message_id}}}
        return self._post(  # type: ignore[return-value]
            f"/chat/getBase64FromMediaMessage/{instance_name}", body
        )

    def fetch_profile_picture_url(
        self,
        instance_name: str,
        number: str,
    ) -> str | None:
        """Busca a URL da foto de perfil do contato (ou None se privada/ausente)."""
        try:
            data = self._post(
                f"/chat/fetchProfilePictureUrl/{instance_name}",
                {"number": number},
            )
        except Exception:
            return None
        if isinstance(data, dict):
            url = data.get("profilePictureUrl") or data.get("url")
            return url or None
        return None

    def send_media(
        self,
        instance_name: str,
        to_number: str,
        media_b64: str,
        *,
        mediatype: str,          # "image" | "document" | "video"
        mimetype: str,
        file_name: str,
        caption: str = "",
    ) -> dict[str, Any]:
        """Envia mídia (imagem/documento/vídeo) via Evolution — base64."""
        log.info(
            "evolution_send_media",
            instance=instance_name,
            to_last4=to_number[-4:] if len(to_number) >= 4 else "??",
            mediatype=mediatype, file_name=file_name,
        )
        body: dict[str, Any] = {
            "number": to_number,
            "mediatype": mediatype,
            "mimetype": mimetype,
            "media": media_b64,
            "fileName": file_name,
        }
        if caption:
            body["caption"] = caption
        return self._post(f"/message/sendMedia/{instance_name}", body)  # type: ignore[return-value]

    def send_audio(
        self,
        instance_name: str,
        to_number: str,
        audio_b64: str,
    ) -> dict[str, Any]:
        """Envia áudio de voz (PTT) via Evolution — base64."""
        log.info(
            "evolution_send_audio",
            instance=instance_name,
            to_last4=to_number[-4:] if len(to_number) >= 4 else "??",
        )
        body: dict[str, Any] = {"number": to_number, "audio": audio_b64}
        return self._post(f"/message/sendWhatsAppAudio/{instance_name}", body)  # type: ignore[return-value]
