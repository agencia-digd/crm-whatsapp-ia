"""
Testes unitários para os dois fixes de get_overdue_tasks:
- Fix 1: filtro local de data (API ignora filter[task_date][to])
- Fix 2: filtro de pipeline (descarta tarefas de leads de outros pipelines)
"""
import json
import time
from unittest.mock import MagicMock, patch

import httpx

PIPELINE_ID = 10000004


def _mock_resp(status: int, body: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.headers = httpx.Headers({})
    resp.json.return_value = body or {}
    resp.text = json.dumps(body or {})
    if status >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status}", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


def _task(task_id: int, entity_id: int, complete_till: int, entity_type: str = "leads") -> dict:
    return {
        "id": task_id,
        "text": f"Tarefa {task_id}",
        "complete_till": complete_till,
        "entity_id": entity_id,
        "entity_type": entity_type,
        "is_completed": False,
    }


def _make_client() -> "KommoTool":  # noqa: F821
    from app.tools.kommo import KommoTool
    return KommoTool()


# ── Fix 1: filtro local de data ───────────────────────────────────────────────

def test_filtra_tarefas_futuras_localmente() -> None:
    """Tarefa com prazo no futuro deve ser descartada mesmo que a API a retorne."""
    now = int(time.time())
    past_task = _task(1, 11111, now - 86400)    # venceu ontem → mantém
    future_task = _task(2, 22222, now + 86400)  # vence amanhã → descarta

    tasks_body = {"_embedded": {"tasks": [past_task, future_task]}}
    # Leads lookup: só lead 11111 existe no Acme (a tarefa futura não passou pro lookup)
    leads_body = {"_embedded": {"leads": [{"id": 11111}]}}

    with patch("app.integrations.kommo_client.httpx.Client") as mock_cls:
        mock_http = MagicMock()
        mock_cls.return_value.__enter__ = MagicMock(return_value=mock_http)
        mock_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_http.get.side_effect = [_mock_resp(200, tasks_body), _mock_resp(200, leads_body)]

        tool = _make_client()
        result = json.loads(tool.get_overdue_tasks(10000003))

    assert len(result) == 1
    assert result[0]["entity_id"] == 11111


def test_filtra_tarefas_is_completed() -> None:
    """Tarefa marcada como completed deve ser descartada mesmo com prazo vencido."""
    now = int(time.time())
    completed_task = {
        "id": 10,
        "text": "Já feita",
        "complete_till": now - 86400,
        "entity_id": 33333,
        "entity_type": "leads",
        "is_completed": True,   # API retornou mesmo com filter[is_completed]=0 — filtramos local
    }
    open_task = _task(11, 44444, now - 86400)

    tasks_body = {"_embedded": {"tasks": [completed_task, open_task]}}
    leads_body = {"_embedded": {"leads": [{"id": 44444}]}}

    with patch("app.integrations.kommo_client.httpx.Client") as mock_cls:
        mock_http = MagicMock()
        mock_cls.return_value.__enter__ = MagicMock(return_value=mock_http)
        mock_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_http.get.side_effect = [_mock_resp(200, tasks_body), _mock_resp(200, leads_body)]

        tool = _make_client()
        result = json.loads(tool.get_overdue_tasks(10000003))

    # completed_task não passa pelo filtro de is_completed
    assert len(result) == 1
    assert result[0]["entity_id"] == 44444


# ── Fix 2: filtro de pipeline ─────────────────────────────────────────────────

def test_filtra_tarefas_de_outros_pipelines() -> None:
    """Tarefa vinculada a lead de outro pipeline (ex: Escala Imóveis) é descartada."""
    now = int(time.time())
    cross_pipeline_task = _task(20, 99999, now - 86400)  # lead 99999 não é Acme

    tasks_body = {"_embedded": {"tasks": [cross_pipeline_task]}}
    # API retorna lista vazia — lead 99999 não existe no pipeline Acme
    leads_body = {"_embedded": {"leads": []}}

    with patch("app.integrations.kommo_client.httpx.Client") as mock_cls:
        mock_http = MagicMock()
        mock_cls.return_value.__enter__ = MagicMock(return_value=mock_http)
        mock_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_http.get.side_effect = [_mock_resp(200, tasks_body), _mock_resp(200, leads_body)]

        tool = _make_client()
        result = json.loads(tool.get_overdue_tasks(10000003))

    assert result == []


def test_combinado_pipeline_e_data() -> None:
    """
    Cenário combinado:
    - task A: vencida + lead no Acme      → MANTÉM
    - task B: futura + lead no Acme       → descarta (data)
    - task C: vencida + lead outro pipeline → descarta (pipeline)
    """
    now = int(time.time())
    task_a = _task(30, 111, now - 86400)   # vencida, Acme
    task_b = _task(31, 222, now + 86400)   # futura, Acme
    task_c = _task(32, 333, now - 86400)   # vencida, outro pipeline

    tasks_body = {"_embedded": {"tasks": [task_a, task_b, task_c]}}
    # task_b descartada antes do lookup (futuro) → lookup com [111, 333]
    # Acme só tem lead 111
    leads_body = {"_embedded": {"leads": [{"id": 111}]}}

    with patch("app.integrations.kommo_client.httpx.Client") as mock_cls:
        mock_http = MagicMock()
        mock_cls.return_value.__enter__ = MagicMock(return_value=mock_http)
        mock_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_http.get.side_effect = [_mock_resp(200, tasks_body), _mock_resp(200, leads_body)]

        tool = _make_client()
        result = json.loads(tool.get_overdue_tasks(10000003))

    assert len(result) == 1
    assert result[0]["entity_id"] == 111
    assert result[0]["dias_atrasada"] >= 1
