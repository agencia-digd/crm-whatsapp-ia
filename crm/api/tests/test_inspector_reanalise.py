"""
Testes da reanálise sob demanda (app/inspector/reanalise.py) — mock do
LLMProvider (padrão dos testes do inspector), zero chamadas reais de LLM,
contexto e conversa injetados por monkeypatch (zero DB/Evolution).
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from app.inspector import reanalise
from app.llm.provider import TokenUsage


def _classifier_provider(temperatura: str = "quente", deserves: bool = True) -> MagicMock:
    provider = MagicMock()
    provider.complete.return_value = (
        json.dumps({
            "temperatura": temperatura,
            "deserves_deep_analysis": deserves,
            "reason": "lead pediu detalhes do imóvel",
        }),
        TokenUsage(100, 50),
    )
    return provider


def _analyzer_provider() -> MagicMock:
    provider = MagicMock()
    provider.complete.return_value = (
        json.dumps({
            "conversa_resumo": "Lead pediu 3 quartos no centro.",
            "o_que_corretora_disse": "posso te mandar fotos",
            "gap_identificado": "Não tentou agendar visita",
            "abordagem_sugerida": "Que horário fica bom pra você visitar?",
        }),
        TokenUsage(500, 200),
    )
    return provider


_CTX = {
    "lead_id": "10000016",
    "lead_nome": "Corretor Paula Lima",
    "telefone": "+5500000000000",
    "corretora_id": 3,
    "corretora_nome": "Pessoa3",
    "instance": "acme_corretora_a",
    "etapa_kommo": "Em negociação",
}


def _conv_recente() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {"from": "corretora", "text": "Oi, vi seu interesse",
         "at": (now - timedelta(hours=2)).isoformat(), "type": "text"},
        {"from": "lead", "text": "Sim, preciso de 3 quartos no centro",
         "at": (now - timedelta(hours=1)).isoformat(), "type": "text"},
        {"from": "corretora", "text": "Temos sim, posso te mandar fotos",
         "at": (now - timedelta(minutes=30)).isoformat(), "type": "text"},
    ]


class TestReanalisarLead:
    def test_lead_ok_grava_e_retorna_raiox(self, monkeypatch) -> None:
        salvos: list = []
        monkeypatch.setattr(reanalise, "_resolver_contexto", lambda lid: dict(_CTX))
        monkeypatch.setattr(reanalise, "_buscar_conversa", lambda ctx: _conv_recente())
        monkeypatch.setattr(
            "app.inspector.pipeline._save_analysis", lambda output: salvos.append(output)
        )

        result = reanalise.reanalisar_lead(
            "10000016",
            classifier_provider=_classifier_provider(),
            analyzer_provider=_analyzer_provider(),
        )

        assert "erro" not in result
        assert result["temperatura"] == "quente"
        assert result["status"] == "atendido"  # última msg é da corretora
        assert result["resumo"] == "Lead pediu 3 quartos no centro."
        assert result["gap"] == "Não tentou agendar visita"
        assert result["sugestao"] == "Que horário fica bom pra você visitar?"
        assert result["dias_sem_msg_lead"] == 0
        assert result["analyzed_at"]

        assert len(salvos) == 1
        output = salvos[0]
        assert output.lead_id == "10000016"
        assert output.entrou_por == "reanalise_manual"
        assert output.analysis_mode == "conversa_whatsapp"
        assert output.corretora == "Pessoa3"
        assert output.corretora_id == 3
        assert output.tarefas_kommo_atrasadas == []
        assert output.etapa_kommo_desatualizada is False

    def test_sem_conversa_retorna_erro_sem_gravar(self, monkeypatch) -> None:
        salvos: list = []
        monkeypatch.setattr(reanalise, "_resolver_contexto", lambda lid: dict(_CTX))
        monkeypatch.setattr(reanalise, "_buscar_conversa", lambda ctx: [])
        monkeypatch.setattr(
            "app.inspector.pipeline._save_analysis", lambda output: salvos.append(output)
        )

        result = reanalise.reanalisar_lead("10000016")

        assert result == {"erro": "sem_conversa"}
        assert salvos == []

    def test_tel_sem_cadastro_nao_resolve_nem_grava(self, monkeypatch) -> None:
        resolver = MagicMock()
        monkeypatch.setattr(reanalise, "_resolver_contexto", resolver)

        result = reanalise.reanalisar_lead("tel:+5500000000000")

        assert result == {"erro": "lead_sem_cadastro"}
        resolver.assert_not_called()
