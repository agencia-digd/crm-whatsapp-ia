"""
Testes do analyzer — mock do LLMProvider, zero chamadas reais.
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.inspector.analyzer import (
    DeepAnalysisResult,
    analyze_lead,
    dias_sem_msg_lead,
    validar_respiro,
    verificar_citacao,
)
from app.llm.provider import TokenUsage


def _make_provider(response: dict | None = None, raises: Exception | None = None) -> MagicMock:
    provider = MagicMock()
    if raises is not None:
        provider.complete.side_effect = raises
    else:
        payload = response or {
            "conversa_resumo": "resumo",
            "o_que_corretora_disse": None,
            "gap_identificado": None,
            "abordagem_sugerida": None,
        }
        provider.complete.return_value = (json.dumps(payload), TokenUsage(500, 200))
    return provider


_LEAD = {"lead_id": "99999", "nome": "Teste"}

_CONV_RICA = [
    {"from": "corretora", "text": "Oi, vi seu interesse",
     "at": "2026-05-01T09:00:00+00:00", "type": "text"},
    {"from": "lead", "text": "Sim, preciso de 3 quartos",
     "at": "2026-05-01T09:01:00+00:00", "type": "text"},
    {"from": "lead", "text": "Tem algo no centro?",
     "at": "2026-05-01T09:02:00+00:00", "type": "text"},
    {"from": "corretora", "text": "Temos sim, posso te mandar fotos",
     "at": "2026-05-01T09:05:00+00:00", "type": "text"},
]

_CONV_SO_CORRETORA = [
    {"from": "corretora", "text": "Oi, tudo bem?",
     "at": "2026-05-01T09:00:00+00:00", "type": "text"},
    {"from": "corretora", "text": "Ainda tem interesse?",
     "at": "2026-05-02T09:00:00+00:00", "type": "text"},
]


class TestAnalyzeLead:
    def test_conversa_rica_retorna_resultado(self) -> None:
        provider = _make_provider({
            "conversa_resumo": "Lead pediu 3 quartos no centro.",
            "o_que_corretora_disse": "posso te mandar fotos",
            "gap_identificado": "Não tentou agendar visita",
            "abordagem_sugerida": "Que horário fica bom pra você visitar?",
        })
        # dias_silencio_lead=0 evita o frame de reativação (conversa fixa é antiga).
        result = analyze_lead(_LEAD, _CONV_RICA, provider=provider, dias_silencio_lead=0)
        assert result is not None
        assert result.conversa_resumo != ""
        assert result.gap_identificado is not None
        # citação bate ("posso te mandar fotos" na conversa) e respiro válido → 1 chamada
        provider.complete.assert_called_once()

    def test_sem_msg_lead_retorna_none_sem_chamar_provider(self) -> None:
        provider = _make_provider()
        result = analyze_lead(_LEAD, _CONV_SO_CORRETORA, provider=provider)
        assert result is None
        provider.complete.assert_not_called()

    def test_conversa_vazia_retorna_none_sem_chamar_provider(self) -> None:
        provider = _make_provider()
        result = analyze_lead(_LEAD, [], provider=provider)
        assert result is None
        provider.complete.assert_not_called()

    def test_conversa_trivial_gap_e_sugestao_null(self) -> None:
        conv = [
            {"from": "corretora", "text": "Olá!", "at": "2026-05-01T09:00:00+00:00", "type": "text"},
            {"from": "lead", "text": "Oi", "at": "2026-05-01T09:01:00+00:00", "type": "text"},
        ]
        provider = _make_provider({
            "conversa_resumo": "Conversa inicial rápida.",
            "o_que_corretora_disse": None,
            "gap_identificado": None,
            "abordagem_sugerida": None,
        })
        result = analyze_lead(_LEAD, conv, provider=provider)
        assert result is not None
        assert result.gap_identificado is None
        assert result.abordagem_sugerida is None

    def test_tokens_contados(self) -> None:
        provider = MagicMock()
        provider.complete.return_value = (
            json.dumps({"conversa_resumo": "resumo", "o_que_corretora_disse": None,
                        "gap_identificado": None, "abordagem_sugerida": None}),
            TokenUsage(input_tokens=400, output_tokens=150),
        )
        result = analyze_lead(_LEAD, _CONV_RICA, provider=provider)
        assert result is not None
        assert result.tokens_used == 550

    def test_output_invalido_retorna_none(self) -> None:
        provider = MagicMock()
        provider.complete.return_value = (json.dumps({"campo_errado": "valor"}), TokenUsage(100, 50))
        result = analyze_lead(_LEAD, _CONV_RICA, provider=provider)
        assert result is None

    def test_excecao_provider_retorna_none(self) -> None:
        provider = _make_provider(raises=RuntimeError("CLI down"))
        result = analyze_lead(_LEAD, _CONV_RICA, provider=provider)
        assert result is None


# ── I4: staleness ──────────────────────────────────────────────────────────────

def _conv_com_lead_ha(dias: int) -> list[dict]:
    """Conversa em que a última msg do LEAD foi há `dias`; corretora respondeu depois."""
    lead_at = datetime.now(timezone.utc) - timedelta(days=dias)
    corretora_at = lead_at + timedelta(minutes=5)
    return [
        {"from": "lead", "text": "Quero apto 2 quartos Bairro X",
         "at": lead_at.isoformat(), "type": "text"},
        {"from": "corretora", "text": "Perfeito, vou te mandar opções",
         "at": corretora_at.isoformat(), "type": "text"},
    ]


class TestStaleness:
    def test_dias_sem_msg_lead_calcula_da_ultima_do_lead(self) -> None:
        # última msg do LEAD há 20 dias, corretora respondeu depois → conta 20, não 0
        conv = _conv_com_lead_ha(20)
        assert dias_sem_msg_lead(conv) == 20

    def test_dias_sem_msg_lead_none_quando_lead_nunca_falou(self) -> None:
        conv = [{"from": "corretora", "text": "Oi", "at": "2026-05-01T09:00:00+00:00", "type": "text"}]
        assert dias_sem_msg_lead(conv) is None

    def test_frame_reativacao_injetado_quando_silencio_alto(self) -> None:
        provider = _make_provider({
            "conversa_resumo": "Lead sumido.",
            "o_que_corretora_disse": None,
            "gap_identificado": None,
            "abordagem_sugerida": "Faz um tempinho, ainda procura apto?",
        })
        result = analyze_lead(_LEAD, _CONV_RICA, provider=provider, dias_silencio_lead=40)
        assert result is not None
        assert result.dias_sem_msg_lead == 40
        # o system prompt da chamada deve conter o frame de reativação
        system_arg = provider.complete.call_args[0][0]
        assert "REATIVAÇÃO" in system_arg
        assert "que bom te ter aqui" in system_arg

    def test_frame_nao_injetado_quando_silencio_baixo(self) -> None:
        provider = _make_provider({
            "conversa_resumo": "Conversa fluindo.",
            "o_que_corretora_disse": None,
            "gap_identificado": None,
            "abordagem_sugerida": "Qual te chamou mais atenção?",
        })
        result = analyze_lead(_LEAD, _CONV_RICA, provider=provider, dias_silencio_lead=3)
        assert result is not None
        system_arg = provider.complete.call_args[0][0]
        assert "CONTEXTO DE STALENESS" not in system_arg


# ── I5.2: respiro validator ─────────────────────────────────────────────────────

class TestRespiroValidator:
    def test_uma_pergunta_no_fim_valida(self) -> None:
        assert validar_respiro("Robson, você busca pra morar ou investir?") is True

    def test_termina_em_afirmacao_invalido(self) -> None:
        assert validar_respiro("Vou te mandar as opções agora.") is False

    def test_duas_perguntas_invalido(self) -> None:
        assert validar_respiro("Quer visitar? E qual bairro prefere?") is False

    def test_none_ou_vazio_nao_bloqueia(self) -> None:
        assert validar_respiro(None) is True
        assert validar_respiro("   ") is True

    def test_pergunta_no_meio_sem_pergunta_final_invalido(self) -> None:
        assert validar_respiro("Você viu? Então segue as fotos.") is False

    def test_respiro_invalido_dispara_retry_e_marca_flag(self) -> None:
        # mock sempre devolve abordagem que termina em afirmação → falha, retry, aceita com flag
        provider = _make_provider({
            "conversa_resumo": "resumo",
            "o_que_corretora_disse": None,
            "gap_identificado": None,
            "abordagem_sugerida": "Vou te mandar as opções.",
        })
        result = analyze_lead(_LEAD, _CONV_RICA, provider=provider, dias_silencio_lead=0)
        assert result is not None
        assert result.respiro_valido is False
        assert provider.complete.call_count == 2  # 1 base + 1 guardrail retry


# ── I6.1: citação-existe ────────────────────────────────────────────────────────

class TestVerificarCitacao:
    _CONV_RENDER = (
        "[2026-05-01 09:00] CORRETORA: Posso te mandar as fotos do apartamento no centro\n"
        "[2026-05-01 09:01] LEAD: Sim, quero ver as fotos"
    )

    def test_citacao_literal_aceita(self) -> None:
        assert verificar_citacao("Posso te mandar as fotos", self._CONV_RENDER) is True

    def test_parafrase_leve_aceita(self) -> None:
        # mesmas palavras significativas, ordem diferente / conector trocado
        assert verificar_citacao("mandar fotos do apartamento", self._CONV_RENDER) is True

    def test_citacao_inventada_rejeita(self) -> None:
        assert verificar_citacao(
            "Vou agendar sua vistoria no cartório amanhã cedo", self._CONV_RENDER
        ) is False

    def test_none_ou_vazio_aceita(self) -> None:
        assert verificar_citacao(None, self._CONV_RENDER) is True
        assert verificar_citacao("", self._CONV_RENDER) is True

    def test_citacao_inventada_no_analyzer_vira_none(self) -> None:
        provider = _make_provider({
            "conversa_resumo": "resumo",
            "o_que_corretora_disse": "Vou agendar vistoria no cartório amanhã cedo",
            "gap_identificado": None,
            "abordagem_sugerida": "Qual horário fica bom?",
        })
        # _CONV_RICA não contém "vistoria/cartório" → citação falha, retry, vira None
        result = analyze_lead(_LEAD, _CONV_RICA, provider=provider, dias_silencio_lead=0)
        assert result is not None
        assert result.citacao_verificada is False
        assert result.o_que_corretora_disse is None
        assert provider.complete.call_count == 2
