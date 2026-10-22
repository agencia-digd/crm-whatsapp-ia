"""Fixtures do V2 sem banco, CRM ou imports da suíte produtiva."""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any

import pytest

from app.inspector_v2.models import ProviderResponse, RedactedLead, Usage
from app.inspector_v2.providers import ShadowProvider


class FakeProvider(ShadowProvider):
    name = "fake"
    model = "deterministic"

    def __init__(self, *, invalid: bool = False, no_citations: bool = False) -> None:
        self.invalid = invalid
        self.no_citations = no_citations
        self.calls: list[str] = []
        self.schemas: list[dict[str, Any]] = []

    def preflight(self) -> dict[str, str | bool | None]:
        return {"available": True, "model": self.model}

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> ProviderResponse:
        self.calls.append(user)
        self.schemas.append(schema)
        if self.invalid:
            return ProviderResponse(content="não-json")
        raw_lines = re.findall(r"\[#(\d{6})\]\s+([^\n]+)", user)
        if raw_lines:
            evidence = [(int(index), raw) for index, raw in raw_lines[:3]]
        else:
            ids = re.findall(r'"mensagem_id":(\d+).*?"citacao":"([^"]+)"', user)
            evidence = [(int(index), quote) for index, quote in ids[:3]]
        while len(evidence) < 3:
            evidence.append((len(evidence) + 1, f"mensagem {len(evidence) + 1}"))
        citations = [] if self.no_citations else [
            {"mensagem_id": index, "trecho": raw, "sustenta": "temperatura atual"}
            for index, raw in evidence[:3]
        ]
        intent_evidence = [] if self.no_citations else [
            {"mensagem_id": index, "data": None, "citacao": raw, "leitura": "sinal observado"}
            for index, raw in evidence[:3]
        ]
        payload = {
            "temperatura_atual": "morno",
            "intencao_maxima": "media",
            "resumo_jornada": "Jornada analisada integralmente.",
            "evidencias_intencao": intent_evidence,
            "financeiro": [],
            "objecoes": [],
            "perguntas_sem_resposta": [],
            "visita": "nao_proposta",
            "gap_corretora": None,
            "proxima_acao": "Retomar de forma consultiva.",
            "proxima_acao_requer_revisao_humana": True,
            "acao_externa_executada": False,
            "citacoes": citations,
            "confianca": "media",
        }
        return ProviderResponse(content=json.dumps(payload), usage=Usage(total_tokens=10))


@pytest.fixture
def lead_factory():
    def make(count: int = 3, alias_suffix: str = "AAAAAAAAAA") -> RedactedLead:
        messages = [
            f"[2026-08-{(index % 20) + 1:02d} 10:{index % 60:02d}] "
            f"{'LEAD' if index % 2 else 'CORRETORA'} [text]: mensagem {index}"
            for index in range(1, count + 1)
        ]
        return RedactedLead(
            lead_alias=f"LEAD-{alias_suffix}",
            corretora_alias="CORRETORA-BBBBBBBBBB",
            mensagens=messages,
            mensagens_total=count,
            faixa_jornada="longa" if count > 80 else "curta",
            temperatura_referencia="morno",
            redactions_count=0,
        )
    return make


@pytest.fixture
def temp_store(tmp_path: Path):
    from app.inspector_v2.storage import ShadowStore

    root = Path(tempfile.mkdtemp(prefix="acme-painel-test-", dir="/tmp"))
    return ShadowStore(root)
