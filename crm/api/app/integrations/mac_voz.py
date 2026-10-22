"""Voz da Assistente no Mac Mini — anúncio de lead novo pela caixinha da sala agencia.

Substitui o caminho Alexa/Voice Monkey (decisão Dona 26/07): a Echo foi pro
quarto, então o aviso de lead sai pela VOZ da Assistente no Mac Mini, que fica ligado
24h na sala. O VPS alcança o Mac via Vpn e dispara `say` por SSH.

Injeção-proof: o texto vai pelo STDIN do `say` remoto (sem interpolar no shell),
então nome de lead com aspas/acento/`;` não quebra nada.

Setup:
1. Mac Mini ligado, com a saída de áudio na caixinha da sala.
2. VPS com chave SSH pro Mac (contato@example.com) — já configurado.
3. MAC_VOZ_ENABLED=true no .env pra ligar (default desligado = deploy seguro).
"""
from __future__ import annotations

import asyncio
import re

import structlog

from app.config import settings

log = structlog.get_logger()

# Correções de pronúncia pra voz PT-BR (Luciana lê "lead" com sotaque errado).
# Aplicadas SÓ no texto falado — o texto original (CRM, logs) fica intacto.
# Pedido da Dona 26/07: "lead" fala-se "líde".
_PRONUNCIA = [
    (re.compile(r"\bleads\b", re.IGNORECASE), "lídes"),
    (re.compile(r"\blead\b", re.IGNORECASE), "líde"),
]


def _pronunciar(text: str) -> str:
    """Reescreve o texto pra soar certo na voz PT-BR (não muda logs/CRM)."""
    for pattern, repl in _PRONUNCIA:
        text = pattern.sub(repl, text)
    return text


async def falar(text: str) -> bool:
    """Faz o Mac Mini falar `text` na caixinha da sala. Best-effort (nunca levanta)."""
    if not settings.mac_voz_enabled:
        log.info("mac_voz_skip_disabled")
        return False

    if settings.dry_run:
        log.info("[DRY_RUN] mac_voz_falar", preview=text[:120])
        return True

    # Toque discreto antes da voz (pra não assustar) + a fala, num único SSH.
    # O texto vai por STDIN do `say` (injeção-proof); o toque é um som de sistema.
    remote = "say -v {voice}".format(voice=settings.mac_voz_voice)
    if settings.mac_voz_chime:
        remote = (
            "afplay /System/Library/Sounds/{chime}.aiff 2>/dev/null; "
            "sleep 0.4; {say}".format(chime=settings.mac_voz_chime, say=remote)
        )

    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh",
            "-o", "ConnectTimeout=8",
            "-o", "BatchMode=yes",
            settings.mac_voz_ssh,
            remote,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(
            proc.communicate(input=_pronunciar(text).encode("utf-8")), timeout=20
        )
        if proc.returncode == 0:
            log.info("mac_voz_falou", context=text[:80])
            return True
        log.warning(
            "mac_voz_falhou",
            returncode=proc.returncode,
            stderr=stderr.decode("utf-8", "replace")[:200],
        )
        return False
    except Exception as exc:
        log.warning("mac_voz_erro", error=str(exc))
        return False
