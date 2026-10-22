"""Onda P2.1 — get_overdue_tasks_in_pipeline filtra cross-pipeline + Won/Lost."""
from __future__ import annotations

from unittest.mock import patch

from app.integrations.kommo_client import KommoClient


def _make_client():
    return KommoClient(subdomain="x", token="y")


def test_get_overdue_tasks_in_pipeline_filtra_pipeline():
    """Tarefas de leads fora do pipeline são descartadas."""
    leads_pipeline = [
        {"id": 100, "status_id": 10000001},  # Follow-up
        {"id": 101, "status_id": 10000002},  # visita agendada
    ]
    tasks_all = [
        {"id": 1, "entity_id": 100, "entity_type": "leads"},  # Acme
        {"id": 2, "entity_id": 200, "entity_type": "leads"},  # outro pipeline
        {"id": 3, "entity_id": 101, "entity_type": "leads"},  # Acme
        {"id": 4, "entity_id": 999, "entity_type": "contacts"},  # outro entity_type
    ]
    c = _make_client()
    with patch.object(c, "get_leads", return_value=iter(leads_pipeline)), \
         patch.object(c, "get_tasks", return_value=tasks_all):
        out = c.get_overdue_tasks_in_pipeline(user_id=10000003, pipeline_id=10000004)
    assert len(out) == 2
    assert {t["id"] for t in out} == {1, 3}


def test_get_overdue_tasks_exclui_won_lost():
    """Leads em status Won (142) ou Lost (143) NÃO contam."""
    leads_pipeline = [
        {"id": 100, "status_id": 10000001},  # Follow-up — ativo
        {"id": 101, "status_id": 142},        # Venda ganha — excluir
        {"id": 102, "status_id": 143},        # Venda perdida — excluir
        {"id": 103, "status_id": 10000002},   # visita agendada — ativo
    ]
    tasks_all = [
        {"id": 1, "entity_id": 100, "entity_type": "leads"},
        {"id": 2, "entity_id": 101, "entity_type": "leads"},
        {"id": 3, "entity_id": 102, "entity_type": "leads"},
        {"id": 4, "entity_id": 103, "entity_type": "leads"},
    ]
    c = _make_client()
    with patch.object(c, "get_leads", return_value=iter(leads_pipeline)), \
         patch.object(c, "get_tasks", return_value=tasks_all):
        out = c.get_overdue_tasks_in_pipeline(
            user_id=10000003, pipeline_id=10000004, exclude_final_statuses=True,
        )
    assert {t["id"] for t in out} == {1, 4}


def test_get_overdue_tasks_pode_incluir_final_se_pedido():
    """exclude_final_statuses=False mantém Won/Lost."""
    leads_pipeline = [
        {"id": 100, "status_id": 142},  # Venda ganha
        {"id": 101, "status_id": 10000001},
    ]
    tasks_all = [
        {"id": 1, "entity_id": 100, "entity_type": "leads"},
        {"id": 2, "entity_id": 101, "entity_type": "leads"},
    ]
    c = _make_client()
    with patch.object(c, "get_leads", return_value=iter(leads_pipeline)), \
         patch.object(c, "get_tasks", return_value=tasks_all):
        out = c.get_overdue_tasks_in_pipeline(
            user_id=10000003, pipeline_id=10000004, exclude_final_statuses=False,
        )
    assert {t["id"] for t in out} == {1, 2}


def test_get_leads_chamado_com_filtros_corretos():
    """get_leads recebe filter[pipeline_id] e filter[responsible_user_id][]."""
    leads_pipeline: list[dict] = []
    tasks_all: list[dict] = []
    c = _make_client()
    captured: dict = {}

    def fake_get_leads(filters, with_contacts=True, limit=250):
        captured["filters"] = filters
        return iter(leads_pipeline)

    with patch.object(c, "get_leads", side_effect=fake_get_leads), \
         patch.object(c, "get_tasks", return_value=tasks_all):
        c.get_overdue_tasks_in_pipeline(user_id=10000003, pipeline_id=10000004)

    assert captured["filters"]["filter[pipeline_id]"] == 10000004
    assert captured["filters"]["filter[responsible_user_id][]"] == 10000003
