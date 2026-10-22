"""Voice Monkey client — dispara Routine Alexa pra anunciar lead novo.

Modelo Routine Trigger: o texto vive na Routine criada no app Alexa
(When sensor `leadacme` is detected → Anunciar "entrou um Lead novo
na Acme"). API só dispara o trigger; texto é fixo na Routine.

Setup:
1. voicemonkey.io → criar Routine Trigger `leadacme`
2. App Alexa celular → Routine: When leadacme → Anunciar "..."
3. VOICEMONKEY_TOKEN + VOICEMONKEY_DEVICE no .env
"""
from __future__ import annotations

import httpx
import structlog

from app.config import settings

log = structlog.get_logger()

_API = "https://api-v2.voicemonkey.io/trigger"


async def announce(text: str) -> bool:
    # Dona 26/07: a Echo foi pro quarto — o aviso de lead agora sai pela voz da
    # Assistente no Mac Mini. Quando MAC_VOZ_ENABLED=true, roteia pro Mac e ignora o
    # Voice Monkey/Alexa (que fica de fallback se um dia religar).
    if settings.mac_voz_enabled:
        from app.integrations import mac_voz
        return await mac_voz.falar(text)

    if not settings.voicemonkey_token or not settings.voicemonkey_device:
        log.info("alexa_skip_no_config", reason="token/device vazio")
        return False

    if settings.dry_run:
        log.info("[DRY_RUN] alexa_announce", preview=text[:120])
        return True

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_API, params={
                "token": settings.voicemonkey_token,
                "device": settings.voicemonkey_device,
            })
            resp.raise_for_status()
        log.info("alexa_triggered", context=text[:80])
        return True
    except Exception as exc:
        log.warning("alexa_announce_failed", error=str(exc))
        return False
