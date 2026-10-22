"""Preparação determinística e redação de PII antes de qualquer provider."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Iterable

from app.inspector_v2.models import PrivateCorpusLead, RedactedLead, SelectionItem, SelectionManifest
from app.inspector_v2.storage import atomic_write_json, require_private_tmp

CPF_RE = re.compile(r"(?<!\d)\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?!\d)")
CNPJ_RE = re.compile(r"(?<!\d)\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}(?!\d)")
EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
CEP_RE = re.compile(r"(?<!\d)\d{5}-?\d{3}(?!\d)")
DOB_RE = re.compile(r"(?<!\d)\d{1,2}[/-]\d{1,2}[/-](?:19|20)\d{2}(?!\d)")
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?55[\s.-]?)?(?:\(?\d{2}\)?[\s.-]?)?"
    r"(?:9[\s.-]?)?\d{4}[\s.-]?\d{4}(?!\d)"
)
FORM_FIELD_RE = re.compile(
    r"(?im)^(\s*(?:nome|cpf|cnpj|cep|data\s+de\s+nascimento|nascimento|"
    r"e-?mail|telefone|celular|endere[cç]o)\s*:)\s*[^\n]*$"
)
ADDRESS_RE = re.compile(
    r"(?im)^\s*(?:(?:rua|avenida|av\.?|alameda|rodovia|estrada|travessa)\s+)[^\n]+$"
)
_UNSAFE_FIRST_NAMES = {
    "quero", "saber", "lead", "cliente", "teste", "novo", "nova", "sem", "nao", "não",
    "whatsapp", "imovel", "imóvel", "contato", "cadastro", "bom", "boa", "ola", "olá",
    "interessado", "interessada", "informacoes", "informações",
}


def stable_alias(prefix: str, value: str) -> str:
    raw = hashlib.sha256(f"acme-inspector-v2:{value}".encode()).digest()
    digest = base64.b32encode(raw).decode("ascii").rstrip("=")[:10]
    return f"{prefix}-{digest}"


def redact_text(
    text: str,
    lead_name: str = "",
    phone: str = "",
    person_pattern: re.Pattern[str] | None = None,
) -> tuple[str, int]:
    redactions = 0

    def substitute(pattern: re.Pattern[str], replacement: str | Any, value: str) -> str:
        nonlocal redactions
        value, count = pattern.subn(replacement, value)
        redactions += count
        return value

    text = substitute(FORM_FIELD_RE, lambda match: f"{match.group(1)} [DADO_REMOVIDO]", text)
    text = substitute(ADDRESS_RE, "[ENDERECO_REMOVIDO]", text)
    text = substitute(CNPJ_RE, "[CNPJ_REMOVIDO]", text)
    text = substitute(CPF_RE, "[CPF_REMOVIDO]", text)
    text = substitute(EMAIL_RE, "[EMAIL_REMOVIDO]", text)
    text = substitute(CEP_RE, "[CEP_REMOVIDO]", text)
    text = substitute(DOB_RE, "[DATA_NASCIMENTO_REMOVIDA]", text)
    text = substitute(PHONE_RE, "[TELEFONE_REMOVIDO]", text)

    digits = re.sub(r"\D", "", phone or "")
    if len(digits) >= 8:
        for candidate in {digits, digits[-11:], digits[-9:], digits[-8:]}:
            if candidate and candidate in text:
                redactions += text.count(candidate)
                text = text.replace(candidate, "[TELEFONE_REMOVIDO]")

    if person_pattern is not None:
        text, count = person_pattern.subn("[PESSOA]", text)
        redactions += count
    else:
        name = lead_name.strip()
        candidates = {name, *(token for token in re.findall(r"[A-Za-zÀ-ÿ]+", name) if len(token) >= 3)}
        candidates.discard("")
        if candidates:
            pattern = re.compile(
                r"(?i)\b(?:" + "|".join(re.escape(item) for item in sorted(candidates, key=len, reverse=True)) + r")\b"
            )
            text, count = pattern.subn("[PESSOA]", text)
            redactions += count
    return text, redactions


def length_bucket(count: int) -> str:
    if count <= 20:
        return "curta"
    if count <= 80:
        return "media"
    return "longa"


def prepare(rows: list[dict[str, Any]], reference: dict[str, str]) -> list[RedactedLead]:
    private = [PrivateCorpusLead.model_validate(row) for row in rows]
    broker_tokens: set[str] = set()
    for row in private:
        value = row.corretora.strip()
        if len(value) >= 3:
            broker_tokens.add(value)
        broker_tokens.update(token for token in re.findall(r"[A-Za-zÀ-ÿ]+", value) if len(token) >= 3)

    prepared: list[RedactedLead] = []
    for row in private:
        person_tokens = set(broker_tokens)
        current_name = row.nome.strip()
        if 3 <= len(current_name) <= 80:
            person_tokens.add(current_name)
            parts = re.findall(r"[A-Za-zÀ-ÿ]+", current_name)
            if parts and len(parts[0]) >= 3 and parts[0].lower() not in _UNSAFE_FIRST_NAMES:
                person_tokens.add(parts[0])
        person_pattern = re.compile(
            r"(?i)\b(?:" + "|".join(re.escape(item) for item in sorted(person_tokens, key=len, reverse=True)) + r")\b"
        )
        messages: list[str] = []
        redactions = 0
        for message in row.mensagens:
            clean, count = redact_text(message, row.nome, row.telefone, person_pattern)
            messages.append(clean)
            redactions += count
        prepared.append(
            RedactedLead(
                lead_alias=stable_alias("LEAD", row.lead_id),
                corretora_alias=stable_alias("CORRETORA", row.corretora),
                mensagens=messages,
                mensagens_total=len(messages),
                faixa_jornada=length_bucket(len(messages)),
                temperatura_referencia=reference.get(row.lead_id, "sem_referencia"),
                redactions_count=redactions,
            )
        )
    assert_safe(prepared)
    return prepared


def deterministic_sample(rows: list[RedactedLead], size: int = 30) -> list[RedactedLead]:
    strata: dict[tuple[str, str, str], deque[RedactedLead]] = defaultdict(deque)
    for row in sorted(rows, key=lambda item: item.lead_alias):
        strata[(row.corretora_alias, row.faixa_jornada, row.temperatura_referencia)].append(row)
    keys = sorted(strata)
    chosen: list[RedactedLead] = []
    while keys and len(chosen) < min(size, len(rows)):
        next_keys: list[tuple[str, str, str]] = []
        for key in keys:
            if strata[key] and len(chosen) < size:
                chosen.append(strata[key].popleft())
            if strata[key]:
                next_keys.append(key)
        keys = next_keys
    return chosen


def sensitive_matches(text: str) -> list[str]:
    matches: list[str] = []
    for label, pattern in (
        ("cpf", CPF_RE), ("cnpj", CNPJ_RE), ("email", EMAIL_RE),
        ("cep", CEP_RE), ("data_nascimento", DOB_RE), ("telefone", PHONE_RE),
    ):
        if pattern.search(text):
            matches.append(label)
    return matches


def assert_safe(rows: Iterable[RedactedLead]) -> None:
    serialized = json.dumps([row.model_dump(mode="json") for row in rows], ensure_ascii=False)
    residual = sensitive_matches(serialized)
    if residual:
        raise ValueError(f"PII residual detectada: {','.join(residual)}")


def write_prepared_artifacts(
    rows: list[RedactedLead],
    sample: list[RedactedLead],
    output_dir: Path,
    corpus_fingerprint: str,
) -> tuple[Path, Path, Path]:
    root = require_private_tmp(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    corpus_path = root / "corpus-redigido.json"
    sample_path = root / "amostra-30-redigida.json"
    manifest_path = root / "amostra-30-manifesto.json"
    atomic_write_json(corpus_path, [row.model_dump(mode="json") for row in rows])
    atomic_write_json(sample_path, [row.model_dump(mode="json") for row in sample])
    manifest = SelectionManifest(
        seed="round-robin-alias-v1",
        corpus_fingerprint=corpus_fingerprint,
        target_size=len(sample),
        items=[
            SelectionItem(
                lead_alias=row.lead_alias,
                corretora_alias=row.corretora_alias,
                faixa_jornada=row.faixa_jornada,
                temperatura_referencia=row.temperatura_referencia,
                mensagens_total=row.mensagens_total,
                redactions_count=row.redactions_count,
            )
            for row in sample
        ],
    )
    atomic_write_json(manifest_path, manifest.model_dump(mode="json"))
    return corpus_path, sample_path, manifest_path
