"""Silencia a Assistente em conversas que a Assistente (Claude/OpenClaw) tá tocando.

Mecanismo simples por arquivo: `data/assistente_silence.txt`, uma linha por telefone
silenciado no formato `<phone> <expires_unix_ts>`. Linhas expiradas são ignoradas.

Use `scripts/assistente_silence.py <phone> [minutes]` pra silenciar.
"""
from __future__ import annotations

import time
from pathlib import Path

SILENCE_FILE = Path(__file__).resolve().parents[2] / "data" / "assistente_silence.txt"


def _read_entries() -> dict[str, int]:
    if not SILENCE_FILE.exists():
        return {}
    entries: dict[str, int] = {}
    for line in SILENCE_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            continue
        phone, ts = parts
        try:
            entries[phone] = int(ts)
        except ValueError:
            continue
    return entries


def _write_entries(entries: dict[str, int]) -> None:
    SILENCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{phone} {ts}" for phone, ts in entries.items()]
    SILENCE_FILE.write_text("\n".join(lines) + ("\n" if lines else ""))


def is_silenced(phone: str) -> bool:
    now = int(time.time())
    entries = _read_entries()
    expires = entries.get(phone)
    return expires is not None and expires > now


def silence(phone: str, minutes: int = 60) -> int:
    expires = int(time.time()) + minutes * 60
    entries = _read_entries()
    entries[phone] = expires
    _write_entries(entries)
    return expires


def unsilence(phone: str) -> bool:
    entries = _read_entries()
    if phone in entries:
        del entries[phone]
        _write_entries(entries)
        return True
    return False
