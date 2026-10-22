"""Testes unitários — funil_metricas.py v2 (modelo híbrido Coorte + Passagens, Onda P2.2)."""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.integrations.kommo_client import (
    KOMMO_PIPELINE_ACME,
    KOMMO_STAGE_EM_ATENDIMENTO,
    KOMMO_STAGE_VENDA_GANHA,
    KOMMO_STAGE_VISITA_AGENDADA,
    KOMMO_STAGE_VISITA_REALIZADA,
)
from app.services.funil_metricas import (
    FunilMetricas,
    calcular_coorte_aquisicao,
    calcular_passagens_atividade,
    enriquecer_vgv_passagens,
)


def _ev(lead_id: int, stage_to: int, stage_from: int = 0, created_by: int = 999) -> dict[str, Any]:
    return {
        "lead_id": lead_id,
        "created_by": created_by,
        "created_at": 1777600000,
        "stage_from": stage_from,
        "stage_to": stage_to,
        "pipeline_id": KOMMO_PIPELINE_ACME,
    }


class TestCoorteAquisicao:

    def test_entrou_agrupa_por_responsible(self) -> None:
        """Leads criados no mês agrupados por responsible_user_id → entrou_mes correto."""
        mock_client = MagicMock()
        mock_client.get_leads.return_value = [
            {"id": 1, "responsible_user_id": 10000003},
            {"id": 2, "responsible_user_id": 10000003},
            {"id": 3, "responsible_user_id": 10000007},
        ]
        result = calcular_coorte_aquisicao(mock_client, KOMMO_PIPELINE_ACME, 0, 9999999, events=[])
        assert result[10000003]["entrou_mes"] == 2
        assert result[10000007]["entrou_mes"] == 1

    def test_em_atendimento_subconjunto_de_entrou(self) -> None:
        """Lead da coorte com evento em_atendimento → em_atendimento_mes conta; sem evento não conta."""
        mock_client = MagicMock()
        mock_client.get_leads.return_value = [
            {"id": 10, "responsible_user_id": 10000003},
            {"id": 11, "responsible_user_id": 10000003},
        ]
        events = [_ev(10, KOMMO_STAGE_EM_ATENDIMENTO)]
        result = calcular_coorte_aquisicao(mock_client, KOMMO_PIPELINE_ACME, 0, 9999999, events=events)
        assert result[10000003]["entrou_mes"] == 2
        assert result[10000003]["em_atendimento_mes"] == 1

    def test_lead_fora_coorte_nao_conta_em_atendimento(self) -> None:
        """Evento em_atendimento de lead não criado no mês não infla em_atendimento_mes."""
        mock_client = MagicMock()
        mock_client.get_leads.return_value = [
            {"id": 10, "responsible_user_id": 10000003},
        ]
        events = [_ev(99, KOMMO_STAGE_EM_ATENDIMENTO)]  # lead 99 fora da coorte
        result = calcular_coorte_aquisicao(mock_client, KOMMO_PIPELINE_ACME, 0, 9999999, events=events)
        assert result[10000003]["em_atendimento_mes"] == 0


class TestPassagensAtividade:

    def test_atribuida_por_created_by(self) -> None:
        """Passagem em visita_agendada atribuída ao created_by, não ao responsible_user_id."""
        events = [_ev(100, KOMMO_STAGE_VISITA_AGENDADA, created_by=10000003)]
        result = calcular_passagens_atividade(events)
        assert result[10000003]["agendados_mes"] == 1

    def test_exclui_created_by_zero(self) -> None:
        """Eventos com created_by=0 (automação Kommo) são descartados."""
        events = [_ev(100, KOMMO_STAGE_VISITA_AGENDADA, created_by=0)]
        result = calcular_passagens_atividade(events)
        assert result == {}

    def test_distinct_lead_por_etapa(self) -> None:
        """Mesmo lead passando 2x pela mesma etapa conta como 1 (idempotência)."""
        events = [
            _ev(50, KOMMO_STAGE_VISITA_AGENDADA, created_by=10000003),
            _ev(50, KOMMO_STAGE_VISITA_AGENDADA, created_by=10000003),  # duplicado
        ]
        result = calcular_passagens_atividade(events)
        assert result[10000003]["agendados_mes"] == 1


class TestVgvPassagens:

    def test_vgv_atribuido_ao_created_by(self) -> None:
        """VGV soma lead.price do ganho e atribui ao created_by do evento."""
        events = [_ev(200, KOMMO_STAGE_VENDA_GANHA, created_by=10000003)]
        metricas: dict[int, FunilMetricas] = {10000003: FunilMetricas(vendas_kommo_mes=1)}

        mock_client = MagicMock()
        mock_client.get_leads_by_ids.return_value = [{"id": 200, "price": 480000}]

        enriquecer_vgv_passagens(metricas, events, mock_client)

        mock_client.get_leads_by_ids.assert_called_once_with([200])
        assert metricas[10000003].vgv_kommo_mes == Decimal("480000")


class TestGetStatusChangedEvents:

    def test_filtra_por_pipeline_id(self) -> None:
        """get_status_changed_events descarta events de outros pipelines."""
        from app.integrations.kommo_client import KommoClient

        raw_events = [
            {
                "entity_id": 1,
                "created_by": 0,
                "created_at": 1777600000,
                "value_before": [{"lead_status": {"id": 111, "pipeline_id": 10000004}}],
                "value_after":  [{"lead_status": {"id": 222, "pipeline_id": 10000004}}],
            },
            {
                "entity_id": 2,
                "created_by": 0,
                "created_at": 1777600000,
                "value_before": [{"lead_status": {"id": 333, "pipeline_id": 99999}}],
                "value_after":  [{"lead_status": {"id": 444, "pipeline_id": 99999}}],
            },
        ]
        api_response = {"_embedded": {"events": raw_events}, "_links": {}}

        client = KommoClient.__new__(KommoClient)
        with patch.object(client, "_get", return_value=api_response):
            result = client.get_status_changed_events(
                pipeline_id=KOMMO_PIPELINE_ACME,
                from_unix=1777593600,
                to_unix=1780272000,
            )

        assert len(result) == 1
        assert result[0]["lead_id"] == 1

    def test_pagina_ate_esgotar(self) -> None:
        """Pagina seguindo _links.next até não ter mais."""
        from app.integrations.kommo_client import KommoClient

        page1 = {
            "_embedded": {"events": [
                {"entity_id": 10, "created_by": 0, "created_at": 1,
                 "value_before": [{"lead_status": {"id": 1, "pipeline_id": KOMMO_PIPELINE_ACME}}],
                 "value_after":  [{"lead_status": {"id": 2, "pipeline_id": KOMMO_PIPELINE_ACME}}]},
            ]},
            "_links": {"next": {"href": "...page2"}},
        }
        page2 = {
            "_embedded": {"events": [
                {"entity_id": 11, "created_by": 0, "created_at": 2,
                 "value_before": [{"lead_status": {"id": 2, "pipeline_id": KOMMO_PIPELINE_ACME}}],
                 "value_after":  [{"lead_status": {"id": 3, "pipeline_id": KOMMO_PIPELINE_ACME}}]},
            ]},
            "_links": {},
        }

        client = KommoClient.__new__(KommoClient)
        with patch.object(client, "_get", side_effect=[page1, page2]):
            result = client.get_status_changed_events(
                pipeline_id=KOMMO_PIPELINE_ACME,
                from_unix=0,
                to_unix=9999999999,
            )

        assert len(result) == 2
        assert {r["lead_id"] for r in result} == {10, 11}
