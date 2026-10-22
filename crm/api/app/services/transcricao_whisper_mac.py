"""
Transcrição via Whisper rodando no Mac mini M4 (mlx-whisper, large-v3 via Metal).

Endpoint HTTP exposto pelo serviço com.assistente.whisper no Mac, acessível
via Vpn em settings.whisper_mac_url. Sem chave, sem rate limit.

Função pública:
    transcrever_audio_whisper_mac(audio_bytes, mime_type, language="pt")
        → {"texto": str|None, "modelo": str, "erro": str|None}

Drop-in compatível com `transcrever_audio` do transcricao_service.
"""
from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

from app.config import settings

log = structlog.get_logger()

_MODELO = "whisper-mac-large-v3"
_TIMEOUT = 300.0
_CONNECT_TIMEOUT = 5.0
_PROBE_TIMEOUT = 3.0
_PROBE_TTL_S = 60.0

# (monotonic do último probe, Mac estava no ar?) — ver _mac_no_ar().
_probe_cache: tuple[float, bool] | None = None


def _mac_no_ar() -> bool:
    """Probe rápido antes de gastar 5 min esperando um Mac que está dormindo.

    Timeout de conexão não basta: o Vpn ACEITA o TCP na hora mesmo com o
    Mac fora, e a requisição pendura no read até os 300s. Um GET curto separa
    "está no ar" de "aceita socket e não responde".

    Resultado fica em cache por _PROBE_TTL_S pra um backfill de centenas de
    áudios não pagar o probe em cada um.
    """
    global _probe_cache
    agora = time.monotonic()
    if _probe_cache and (agora - _probe_cache[0]) < _PROBE_TTL_S:
        return _probe_cache[1]

    base = settings.whisper_mac_url.rstrip("/")
    try:
        with httpx.Client(timeout=_PROBE_TIMEOUT) as client:
            resp = client.get(f"{base}/health")
        vivo = resp.status_code < 500
    except Exception:  # noqa: BLE001
        vivo = False

    _probe_cache = (agora, vivo)
    if not vivo:
        log.warning("whisper_mac_fora_do_ar", url=base, probe_s=_PROBE_TIMEOUT)
    return vivo


def transcrever_audio_whisper_mac(
    audio_bytes: bytes,
    mime_type: str,
    language: str = "pt",
) -> dict[str, Any]:
    """Envia áudio pro Mac via Vpn e devolve texto."""
    url = f"{settings.whisper_mac_url.rstrip('/')}/transcribe"
    filename = "audio.ogg" if "ogg" in mime_type else "audio.m4a"

    if not _mac_no_ar():
        return {"texto": None, "modelo": _MODELO, "erro": "whisper_mac fora do ar (probe)"}

    # Connect curto (o Mac ou atende na hora, ou está fora): com o timeout único
    # de 300s, o Mac dormindo travava CADA áudio por 5 minutos antes de falhar.
    # Read continua longo — transcrever 2min de áudio no large-v3 demora.
    timeout = httpx.Timeout(_TIMEOUT, connect=_CONNECT_TIMEOUT)

    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                url,
                files={"audio": (filename, audio_bytes, mime_type)},
                params={"language": language},
            )
        elapsed = round(time.monotonic() - t0, 2)
        if resp.status_code != 200:
            log.warning(
                "transcricao_whisper_mac_http_erro",
                status=resp.status_code,
                elapsed_s=elapsed,
                body=resp.text[:200],
            )
            return {"texto": None, "modelo": _MODELO, "erro": f"http {resp.status_code}: {resp.text[:200]}"}

        data = resp.json()
        texto = (data.get("text") or "").strip() or None
        log.info(
            "transcricao_whisper_mac_ok",
            bytes=len(audio_bytes),
            elapsed_s=elapsed,
            duration_s=data.get("duration"),
            texto_len=len(texto) if texto else 0,
        )
        return {"texto": texto, "modelo": _MODELO, "erro": None}

    except Exception as exc:
        elapsed = round(time.monotonic() - t0, 2)
        err_str = str(exc)
        log.warning(
            "transcricao_whisper_mac_erro",
            elapsed_s=elapsed,
            erro=err_str[:200],
        )
        return {"texto": None, "modelo": _MODELO, "erro": err_str}
