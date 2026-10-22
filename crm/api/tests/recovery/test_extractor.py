"""Testa extractor com LLMProvider mockado — valida Pydantic + formato."""
from __future__ import annotations

import json
from datetime import date

from app.llm.provider import TokenUsage
from app.recovery.extractor import QualificationResult, extract
from app.recovery.responder import Message


class FakeProvider:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.called_with: tuple[str, str] | None = None

    def complete(self, system: str, user: str, json_schema=None):
        self.called_with = (system, user)
        return json.dumps(self.payload), TokenUsage(100, 50)


def _hist() -> list[Message]:
    return [
        Message(role="lead", text="oi vi anuncio de vcs ano passado"),
        Message(role="corretor", text="Oi! Aqui é a Corretor. Ainda tá procurando pra comprar?"),
        Message(role="lead", text="sim, na vila augusta"),
        Message(role="corretor", text="Show. Tem FGTS pra entrada?"),
        Message(role="lead", text="tenho uns 40k de fgts"),
        Message(role="corretor", text="Beleza. Faixa de valor?"),
        Message(role="lead", text="até 350 mil"),
    ]


def test_extract_full_qualification() -> None:
    provider = FakeProvider({
        "interesse_compra": "sim",
        "regiao_interesse": "Bairro X",
        "tem_fgts": True,
        "valor_investimento": "até 350k",
        "motivo_perda": None,
        "resumo_nota": (
            "[Corretor · Recuperação 2026-05-21]\n"
            "- Interesse: comprar (Bairro X)\n"
            "- FGTS: sim, ~40k\n"
            "- Valor: até R$350k"
        ),
    })
    result = extract(provider, "João", _hist(), today=date(2026, 5, 21))
    assert isinstance(result, QualificationResult)
    assert result.interesse_compra == "sim"
    assert result.regiao_interesse == "Bairro X"
    assert result.tem_fgts is True
    assert "Bairro X" in result.resumo_nota


def test_extract_aluguel_marks_motivo_perda() -> None:
    provider = FakeProvider({
        "interesse_compra": "nao",
        "regiao_interesse": None,
        "tem_fgts": None,
        "valor_investimento": None,
        "motivo_perda": "aluguel",
        "resumo_nota": "[Corretor · Recuperação 2026-05-21]\nLead buscava aluguel — Acme não atende.",
    })
    result = extract(provider, "Pessoa", _hist())
    assert result.motivo_perda == "aluguel"
    assert result.interesse_compra == "nao"


def test_extract_passes_history_to_provider() -> None:
    provider = FakeProvider({"resumo_nota": "minimal note ok"})
    extract(provider, "Corretor", _hist())
    assert provider.called_with is not None
    _, user_block = provider.called_with
    assert "Bairro X" in user_block or "vila augusta" in user_block
    assert "LEAD" in user_block
    assert "CORRETOR" in user_block
