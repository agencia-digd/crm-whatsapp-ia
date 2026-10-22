"""Leitura exclusiva de corpus redigido e parsing cronológico."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from app.inspector_v2.models import ParsedMessage, RedactedLead, SelectionItem, SelectionManifest

_MESSAGE_RE = re.compile(
    r"^\[(?P<at>\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\s+(?P<sender>LEAD|CORRETORA)"
    r"(?:\s+\[(?P<message_type>[^\]]+)\])?\s*:",
    re.IGNORECASE,
)


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_redacted_corpus(path: Path) -> tuple[list[RedactedLead], str]:
    raw = path.read_bytes()
    decoded = json.loads(raw)
    if not isinstance(decoded, list):
        raise ValueError("corpus redigido deve ser uma lista")
    leads = [RedactedLead.model_validate(item) for item in decoded]
    aliases = [lead.lead_alias for lead in leads]
    if len(aliases) != len(set(aliases)):
        raise ValueError("corpus redigido contém lead_alias duplicado")
    return leads, sha256_bytes(raw)


def deterministic_skip_reason(raw: Any) -> str | None:
    try:
        lead = RedactedLead.model_validate(raw)
    except Exception:
        return "schema_invalido"
    if not lead.mensagens:
        return "sem_mensagens"
    if not any(message.strip() for message in lead.mensagens):
        return "sem_conteudo_utilizavel"
    return None


def parse_messages(messages: Iterable[str], *, sort_chronologically: bool = True) -> list[ParsedMessage]:
    parsed: list[ParsedMessage] = []
    for original_index, raw in enumerate(messages, start=1):
        match = _MESSAGE_RE.match(raw.strip())
        timestamp = datetime.strptime(match.group("at"), "%Y-%m-%d %H:%M") if match else None
        sender = match.group("sender").lower() if match else None
        message_type = match.group("message_type").lower() if match and match.group("message_type") else None
        parsed.append(
            ParsedMessage(
                index=original_index,
                original_index=original_index,
                raw=raw,
                timestamp=timestamp,
                sender=sender,
                message_type=message_type,
                automated=message_type == "template",
            )
        )
    if sort_chronologically:
        parseable = [message for message in parsed if message.timestamp is not None]
        unparseable = [message for message in parsed if message.timestamp is None]
        parseable.sort(key=lambda message: (message.timestamp, message.original_index))
        ordered = [*parseable, *unparseable]
        parsed = [message.model_copy(update={"index": index}) for index, message in enumerate(ordered, start=1)]
    return parsed


def load_selection_manifest(path: Path, corpus_fingerprint: str) -> SelectionManifest:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(decoded, dict) and "items" in decoded:
        manifest = SelectionManifest.model_validate(decoded)
        if manifest.corpus_fingerprint != corpus_fingerprint:
            raise ValueError("manifesto pertence a outro corpus")
        return manifest
    if not isinstance(decoded, list):
        raise ValueError("manifesto deve ser lista ou SelectionManifest")
    items = [
        SelectionItem(
            lead_alias=str(item["lead_alias"]),
            corretora_alias=str(item["corretora_alias"]),
            faixa_jornada=str(item["faixa_jornada"]),
            temperatura_referencia=str(item["temperatura_referencia"]),
            mensagens_total=int(item["mensagens_total"]),
            redactions_count=int(item["redactions_count"]),
        )
        for item in decoded
    ]
    return SelectionManifest(
        seed="round-robin-alias-v1",
        corpus_fingerprint=corpus_fingerprint,
        target_size=len(items),
        items=items,
    )


def manifest_fingerprint(manifest: SelectionManifest) -> str:
    canonical = json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, sort_keys=True).encode("utf-8")
    return sha256_bytes(canonical)
