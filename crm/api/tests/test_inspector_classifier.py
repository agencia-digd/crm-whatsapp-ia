"""
Testes do classifier — mock do LLMProvider, zero chamadas reais.

v2: status é determinístico; LLM decide apenas temperatura + deserves_deep.
"""
import json
from unittest.mock import MagicMock

import pytest

from app.inspector.classifier import (
    ClassificationResult,
    _FALLBACK_LLM,
    _determine_status,
    classify_lead,
)
from app.llm.provider import TokenUsage


def _make_provider(
    temperatura: str = "frio",
    deserves: bool = False,
    reason: str = "ok",
    raises: Exception | None = None,
) -> MagicMock:
    """Mock de LLMProvider — retorna apenas os campos que o LLM agora decide."""
    provider = MagicMock()
    if raises is not None:
        provider.complete.side_effect = raises
    else:
        payload = {
            "temperatura": temperatura,
            "deserves_deep_analysis": deserves,
            "reason": reason,
        }
        provider.complete.return_value = (json.dumps(payload), TokenUsage(100, 50))
    return provider


_LEAD = {"lead_id": "99999", "nome": "Teste", "etapa_nome": "Primeiro Contato"}

# Conversa genérica que termina com a corretora (status = atendido)
_CONV_ATENDIDO = [
    {"from": "lead", "text": "Oi, tenho interesse", "at": "2026-05-01T10:00:00-03:00", "type": "text"},
    {"from": "corretora", "text": "Olá, posso ajudar!", "at": "2026-05-01T10:05:00-03:00", "type": "text"},
]

# Conversa que termina com o lead (status = sem_resposta)
_CONV_SEM_RESPOSTA = [
    {"from": "corretora", "text": "Oi!", "at": "2026-05-01T09:00:00-03:00", "type": "text"},
    {"from": "lead", "text": "Tem financiamento?", "at": "2026-05-01T09:30:00-03:00", "type": "text"},
]


# ── _determine_status — determinístico puro (sem LLM) ────────────────────────

class TestDetermineStatus:
    def test_sem_mensagens_retorna_nao_atendido(self) -> None:
        assert _determine_status([]) == "nao_atendido"

    def test_ultima_msg_do_lead_retorna_sem_resposta(self) -> None:
        conv = [
            {"from": "corretora", "text": "Oi!"},
            {"from": "lead", "text": "Tem financiamento?"},
        ]
        assert _determine_status(conv) == "sem_resposta"

    def test_ultima_msg_da_corretora_retorna_atendido(self) -> None:
        conv = [
            {"from": "lead", "text": "Oi!"},
            {"from": "corretora", "text": "Olá, tudo bem?"},
        ]
        assert _determine_status(conv) == "atendido"

    def test_so_lead_retorna_sem_resposta(self) -> None:
        conv = [{"from": "lead", "text": "Oi"}, {"from": "lead", "text": "Alguém?"}]
        assert _determine_status(conv) == "sem_resposta"

    def test_so_corretora_retorna_atendido(self) -> None:
        conv = [{"from": "corretora", "text": "Oi!"}, {"from": "corretora", "text": "Posso ajudar?"}]
        assert _determine_status(conv) == "atendido"


# ── classify_lead — pipeline completo ────────────────────────────────────────

class TestClassifyLead:
    def test_conversa_vazia_retorna_nao_atendido_sem_llm(self) -> None:
        provider = _make_provider()
        result = classify_lead(_LEAD, [], provider=provider)

        assert result.status == "nao_atendido"
        assert result.deserves_deep_analysis is False
        assert result.tokens_used == 0
        provider.complete.assert_not_called()  # nenhuma chamada ao LLM

    def test_status_determinado_ultima_msg_lead(self) -> None:
        provider = _make_provider(temperatura="quente", deserves=True)
        result = classify_lead(_LEAD, _CONV_SEM_RESPOSTA, provider=provider)
        assert result.status == "sem_resposta"

    def test_status_determinado_ultima_msg_corretora(self) -> None:
        provider = _make_provider(temperatura="quente", deserves=True)
        result = classify_lead(_LEAD, _CONV_ATENDIDO, provider=provider)
        assert result.status == "atendido"

    def test_morno_quando_vou_pensar_apos_engajamento(self) -> None:
        conv = [
            {"from": "lead",      "text": "Quanto é o apto?"},
            {"from": "corretora", "text": "R$ 420.000"},
            {"from": "lead",      "text": "Vou pensar e te aviso"},
            {"from": "corretora", "text": "Beleza!"},
        ]
        provider = _make_provider(temperatura="morno", deserves=False)
        result = classify_lead(_LEAD, conv, provider=provider)
        assert result.temperatura == "morno"

    def test_frio_apenas_sem_engajamento(self) -> None:
        conv = [
            {"from": "lead",      "text": "ok"},
            {"from": "corretora", "text": "Posso ajudar com algo?"},
        ]
        provider = _make_provider(temperatura="frio", deserves=False)
        result = classify_lead(_LEAD, conv, provider=provider)
        assert result.temperatura == "frio"

    def test_deserves_deep_quando_morno(self) -> None:
        # LLM retorna deserves=False, mas override força True para morno
        provider = _make_provider(temperatura="morno", deserves=False)
        result = classify_lead(_LEAD, _CONV_ATENDIDO, provider=provider)
        assert result.deserves_deep_analysis is True

    def test_deserves_deep_quando_quente(self) -> None:
        # LLM retorna deserves=False, mas override força True para quente
        conv = [
            {"from": "lead",      "text": "Quero visitar no sábado!"},
            {"from": "corretora", "text": "Ótimo, confirmo!"},
        ]
        provider = _make_provider(temperatura="quente", deserves=False)
        result = classify_lead(_LEAD, conv, provider=provider)
        assert result.deserves_deep_analysis is True

    def test_frio_nao_ativa_deserves_deep(self) -> None:
        provider = _make_provider(temperatura="frio", deserves=False)
        result = classify_lead(_LEAD, _CONV_ATENDIDO, provider=provider)
        assert result.deserves_deep_analysis is False

    def test_keywords_quentes_temperatura_quente(self) -> None:
        conv = [
            {"from": "corretora", "text": "Olá, temos apartamentos", "at": "...", "type": "text"},
            {"from": "lead",      "text": "Quero agendar uma visita amanhã", "at": "...", "type": "text"},
            {"from": "lead",      "text": "Pode ser às 14h?", "at": "...", "type": "text"},
        ]
        provider = _make_provider(temperatura="quente", deserves=True, reason="lead pediu visita com horário")
        result = classify_lead(_LEAD, conv, provider=provider)
        assert result.temperatura == "quente"
        assert result.deserves_deep_analysis is True

    def test_tokens_contados(self) -> None:
        provider = MagicMock()
        provider.complete.return_value = (
            json.dumps({"temperatura": "frio", "deserves_deep_analysis": False, "reason": "ok"}),
            TokenUsage(input_tokens=200, output_tokens=80),
        )
        result = classify_lead(_LEAD, _CONV_ATENDIDO, provider=provider)
        assert result.tokens_used == 280

    def test_output_invalido_retorna_fallback(self) -> None:
        provider = MagicMock()
        provider.complete.return_value = (json.dumps({"campo_errado": "valor"}), TokenUsage(50, 10))
        result = classify_lead(_LEAD, _CONV_ATENDIDO, provider=provider)
        # Status determinístico é preservado; fallback aplica temperatura frio
        assert result.status == "atendido"  # _CONV_ATENDIDO termina com corretora
        assert result.temperatura == _FALLBACK_LLM.temperatura
        assert result.deserves_deep_analysis is False

    def test_excecao_provider_retorna_fallback(self) -> None:
        provider = _make_provider(raises=RuntimeError("CLI timeout"))
        result = classify_lead(_LEAD, _CONV_ATENDIDO, provider=provider)
        assert result.status == "atendido"
        assert result.temperatura == "frio"
        assert result.tokens_used == 0
