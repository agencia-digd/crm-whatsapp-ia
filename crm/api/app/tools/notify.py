"""
Notificação best-effort pra bridge Telegram da Assistente (telegram-claude).

Usado pelo briefing das 20h (fix E1 da Revisão Fable etapa 4): quando o envio
pra Dona (Telegram) ou pra Pessoa (WhatsApp) falha, a Dona precisa SABER — hoje
a falha é silenciosa (cron `exit 0`) e ninguém percebe.

Contrato do bridge (`/opt/telegram-claude/src/notify_api.py`):
  POST {notify_api_url}
  Header: Authorization: Bearer <NOTIFY_API_TOKEN>
  Body:   {"task_id": str, "text": str, "descricao"?: str, "parse_mode"?: str}

Regras:
- NUNCA derruba o briefing: qualquer erro (bridge offline, timeout, sem token)
  é apenas logado; a função retorna False mas não levanta.
- NUNCA loga o token.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import structlog

from app.config import settings

log = structlog.get_logger()

SP_TZ = ZoneInfo("America/Sao_Paulo")


def _resolve_token() -> str:
    """Token do bridge: settings.notify_api_token OU lido do env file do bridge.

    Best-effort: se o arquivo não existir/não tiver a chave, retorna "".
    Nunca loga o valor.
    """
    if settings.notify_api_token.strip():
        return settings.notify_api_token.strip()

    env_path = Path(settings.notify_bridge_env_file)
    if not env_path.exists():
        return ""
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == "NOTIFY_API_TOKEN":
                return v.strip().strip('"').strip("'")
    except Exception:
        log.warning("notify_token_read_failed", env_file=str(env_path))
    return ""


def notify_dona(task_id: str, text: str, descricao: str) -> bool:
    """Aviso operacional pra Dona via bridge Telegram. Best-effort, nunca levanta.

    Quem chama decide a trava de DRY_RUN/pytest (o briefing das 20h já tem a
    dele; a fila da Pessoa tem a dela em `pessoa_retomada._alertar_dona`).
    """
    token = _resolve_token()
    if not token:
        log.warning("notify_dona_no_token", task_id=task_id)
        return False

    payload = {
        "task_id": task_id,
        "text": text,
        "descricao": descricao,
        "parse_mode": "Markdown",
    }
    try:
        with httpx.Client(timeout=8) as client:
            resp = client.post(
                settings.notify_api_url,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
        if resp.status_code // 100 == 2:
            log.info("notify_dona_sent", task_id=task_id)
            return True
        log.warning("notify_dona_bad_status", task_id=task_id, status=resp.status_code)
        return False
    except Exception as exc:
        # Bridge offline/timeout — apenas loga, não pode derrubar quem chamou.
        log.warning("notify_dona_unreachable", task_id=task_id, error=str(exc))
        return False


def notify_briefing_failure(destino: str, erro: str) -> bool:
    """Avisa a Dona (via bridge Telegram) que o briefing das 20h falhou.

    Args:
        destino: "Dona" | "Pessoa" (a quem o envio que falhou era destinado)
        erro: descrição curta do erro (classe da exceção ou status)

    Returns:
        True se o bridge aceitou (HTTP 2xx), False caso contrário. Nunca levanta.
    """
    hoje = datetime.now(SP_TZ).strftime("%Y%m%d")
    return notify_dona(
        task_id=f"copiloto-briefing-fail-{destino.lower()}-{hoje}",
        text=f"⚠️ Briefing 20h FALHOU pra {destino}: {erro}",
        descricao="Falha no briefing 20h (Copiloto Acme)",
    )
