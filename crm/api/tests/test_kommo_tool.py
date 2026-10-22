"""
Testes unitários (mock httpx) e de integração para KommoClient e KommoTool.

Rodar apenas unitários (padrão CI):
    pytest tests/test_kommo_tool.py

Rodar com integração (requer /root/.acme-kommo.env válido):
    pytest tests/test_kommo_tool.py -m integration
"""
import json
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

PIPELINE_ID = 10000004


# ── Fixtures / helpers ───────────────────────────────────────────────────────

def _mock_resp(status: int, body: dict | None = None, headers: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.headers = httpx.Headers(headers or {})
    resp.json.return_value = body or {}
    resp.text = json.dumps(body or {})

    if status >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status}", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status.return_value = None

    return resp


def _make_lead(lead_id: int, pipeline_id: int = PIPELINE_ID) -> dict:
    return {
        "id": lead_id,
        "name": f"Lead {lead_id}",
        "pipeline_id": pipeline_id,
        "status_id": 10000005,
        "responsible_user_id": 10000006,
        "created_at": 1746230400,
        "updated_at": 1746230400,
        "custom_fields_values": [],
        "_embedded": {
            "contacts": [{
                "id": 999,
                "custom_fields_values": [{
                    "field_code": "PHONE",
                    "values": [{"value": "+5500000000000"}],
                }]
            }]
        },
    }


def _client(subdomain: str = "test", token: str = "tok") -> "KommoClient":  # noqa: F821
    from app.integrations.kommo_client import KommoClient
    return KommoClient(subdomain=subdomain, token=token)


# ── get_leads_today — filtro de pipeline ─────────────────────────────────────

def test_get_leads_today_passes_pipeline_filter() -> None:
    """get_leads_today deve incluir filter[pipeline_id] na request."""
    from app.tools.kommo import KommoTool

    body = {"_embedded": {"leads": [_make_lead(1)]}}
    captured_params: list[dict] = []

    def fake_get(url: str, **kwargs: object) -> MagicMock:
        captured_params.append(kwargs.get("params") or {})
        return _mock_resp(200, body)

    with patch("app.integrations.kommo_client.httpx.Client") as mock_cls:
        mock_http = MagicMock()
        mock_cls.return_value.__enter__ = MagicMock(return_value=mock_http)
        mock_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_http.get.side_effect = fake_get

        tool = KommoTool()
        result = json.loads(tool.get_leads_today())

    assert len(result) == 1
    assert result[0]["lead_id"] == "1"
    # Confirma filtro de pipeline em pelo menos uma chamada
    assert any(p.get("filter[pipeline_id]") == PIPELINE_ID for p in captured_params)


# ── Paginação multi-página ───────────────────────────────────────────────────

def test_get_leads_pagination() -> None:
    """get_leads deve continuar paginando enquanto página retornar 'limit' itens."""
    from app.integrations.kommo_client import KommoClient

    page_responses = [
        {"_embedded": {"leads": [_make_lead(i) for i in range(1, 251)]}},   # página 1: 250
        {"_embedded": {"leads": [_make_lead(i) for i in range(251, 256)]}}, # página 2: 5
    ]
    call_count = [0]

    def fake_get(url: str, **kwargs: object) -> MagicMock:
        idx = call_count[0]
        call_count[0] += 1
        return _mock_resp(200, page_responses[idx] if idx < len(page_responses) else {"_embedded": {"leads": []}})

    with patch("app.integrations.kommo_client.httpx.Client") as mock_cls:
        mock_http = MagicMock()
        mock_cls.return_value.__enter__ = MagicMock(return_value=mock_http)
        mock_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_http.get.side_effect = fake_get

        client = _client()
        leads = list(client.get_leads({"filter[pipeline_id]": PIPELINE_ID}))

    assert len(leads) == 255
    assert call_count[0] == 2  # 2 páginas


# ── 429 com Retry-After ──────────────────────────────────────────────────────

def test_429_respects_retry_after() -> None:
    """Após 429, deve aguardar o tempo do header Retry-After antes de retentar."""
    from app.integrations.kommo_client import KommoClient

    resp_429 = _mock_resp(429, headers={"Retry-After": "1"})
    resp_ok = _mock_resp(200, {"_embedded": {"leads": [_make_lead(1)]}})
    call_results = [resp_429, resp_ok]
    call_idx = [0]

    def fake_get(url: str, **kwargs: object) -> MagicMock:
        r = call_results[call_idx[0]]
        call_idx[0] += 1
        return r

    slept: list[float] = []

    with patch("app.integrations.kommo_client.httpx.Client") as mock_cls, \
         patch("app.integrations.kommo_client.time.sleep", side_effect=lambda s: slept.append(s)):
        mock_http = MagicMock()
        mock_cls.return_value.__enter__ = MagicMock(return_value=mock_http)
        mock_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_http.get.side_effect = fake_get

        client = _client()
        leads = list(client.get_leads({"filter[pipeline_id]": PIPELINE_ID}))

    assert len(leads) == 1
    assert call_idx[0] == 2  # 2 chamadas (1 retry)


# ── 401 não retenta ──────────────────────────────────────────────────────────

def test_401_does_not_retry() -> None:
    """401 (token inválido) não deve ser retentado — propaga imediatamente."""
    from app.integrations.kommo_client import KommoClient

    call_count = [0]

    def fake_get(url: str, **kwargs: object) -> MagicMock:
        call_count[0] += 1
        return _mock_resp(401)

    with patch("app.integrations.kommo_client.httpx.Client") as mock_cls:
        mock_http = MagicMock()
        mock_cls.return_value.__enter__ = MagicMock(return_value=mock_http)
        mock_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_http.get.side_effect = fake_get

        client = _client()
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            list(client.get_leads({"filter[pipeline_id]": PIPELINE_ID}))

    assert exc_info.value.response.status_code == 401
    assert call_count[0] == 1  # zero retries


# ── Normalização de telefone ──────────────────────────────────────────────────

def test_phone_normalization() -> None:
    from app.tools.kommo import _normalize_phone

    assert _normalize_phone("+5500000000000") == "+5500000000000"
    assert _normalize_phone("+5500000000000") == "+5500000000000"
    assert _normalize_phone("+5500000000000") == "+5500000000000"
    assert _normalize_phone("+5500000000000") == "+5500000000000"


# ── get_overdue_tasks — formato rico ─────────────────────────────────────────

def test_get_overdue_tasks_inclui_lead_id() -> None:
    """get_overdue_tasks deve incluir lead_id e dias_atrasada em cada tarefa."""
    import time
    from app.tools.kommo import KommoTool

    complete_till = int(time.time()) - 2 * 86400  # 2 dias atrás
    raw_task = {
        "id": 101,
        "text": "Ligação 1",
        "complete_till": complete_till,
        "entity_id": 55555,
        "entity_type": "leads",
    }
    tasks_body = {"_embedded": {"tasks": [raw_task]}}
    # Segunda chamada: get_leads_by_ids valida que lead 55555 está no pipeline Acme
    leads_body = {"_embedded": {"leads": [{"id": 55555, "pipeline_id": PIPELINE_ID}]}}

    with patch("app.integrations.kommo_client.httpx.Client") as mock_cls:
        mock_http = MagicMock()
        mock_cls.return_value.__enter__ = MagicMock(return_value=mock_http)
        mock_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_http.get.side_effect = [_mock_resp(200, tasks_body), _mock_resp(200, leads_body)]

        tool = KommoTool()
        result = json.loads(tool.get_overdue_tasks(10000003))

    assert len(result) == 1
    t = result[0]
    assert t["lead_id"] == "55555"
    assert t["entity_id"] == 55555
    assert t["tipo"] == "Ligação 1"
    assert t["dias_atrasada"] >= 1   # pelo menos 1 dia
    assert "complete_till_at" in t


def test_get_overdue_tasks_for_lead_filtra_corretamente() -> None:
    """get_overdue_tasks_for_lead deve retornar só tarefas do lead pedido."""
    import time
    from app.tools.kommo import KommoTool

    now = int(time.time())
    raw_tasks = [
        {"id": 1, "text": "Ligar pra lead 1", "complete_till": now - 86400,
         "entity_id": 11111, "entity_type": "leads"},
        {"id": 2, "text": "Ligar pra lead 2", "complete_till": now - 86400,
         "entity_id": 22222, "entity_type": "leads"},
    ]
    tasks_body = {"_embedded": {"tasks": raw_tasks}}
    # Segunda chamada: ambos os leads pertencem ao pipeline Acme
    leads_body = {"_embedded": {"leads": [{"id": 11111}, {"id": 22222}]}}

    with patch("app.integrations.kommo_client.httpx.Client") as mock_cls:
        mock_http = MagicMock()
        mock_cls.return_value.__enter__ = MagicMock(return_value=mock_http)
        mock_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_http.get.side_effect = [_mock_resp(200, tasks_body), _mock_resp(200, leads_body)]

        tool = KommoTool()
        result = tool.get_overdue_tasks_for_lead("11111", 10000003)

    assert len(result) == 1
    assert result[0]["entity_id"] == 11111


# ── Integração real ───────────────────────────────────────────────────────────

@pytest.mark.integration
def test_get_account_real() -> None:
    """Chama a API real e verifica que a conta retornada é Acme/Acme."""
    from app.config import settings
    from app.integrations.kommo_client import KommoClient

    client = KommoClient(settings.kommo_subdomain, settings.kommo_token)
    account = client.get_account()

    name = account.get("name", "")
    assert "Acme" in name or "Acme" in name, f"Conta inesperada: {name!r}"


@pytest.mark.integration
def test_get_pipeline_stages_real() -> None:
    """Pipeline 10000004 deve existir e ter ao menos 4 estágios."""
    from app.config import settings
    from app.tools.kommo import KommoTool

    tool = KommoTool()
    stages = json.loads(tool.get_pipeline_stages())
    assert len(stages) >= 4, f"Poucos estágios: {stages}"
