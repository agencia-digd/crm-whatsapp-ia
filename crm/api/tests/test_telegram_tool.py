from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.config import settings
from app.db.session import SessionLocal
from app.db import models


# ── helpers ────────────────────────────────────────────────────────────────

def _last_briefing() -> models.BriefingSent | None:
    with SessionLocal() as session:
        return (
            session.query(models.BriefingSent)
            .order_by(models.BriefingSent.id.desc())
            .first()
        )


def _make_tool(dry_run: bool) -> "TelegramTool":  # noqa: F821
    """Import here so monkeypatching settings works."""
    from app.tools.telegram import TelegramTool
    settings.dry_run = dry_run
    return TelegramTool()


# ── DRY_RUN ────────────────────────────────────────────────────────────────

def test_dry_run_does_not_call_http(caplog: pytest.LogCaptureFixture) -> None:
    tool = _make_tool(dry_run=True)

    with patch("app.tools.telegram.httpx.Client") as mock_client:
        result = tool.send_to_dona("Olá, teste DRY_RUN")

    assert result == "ok (dry_run)"
    mock_client.assert_not_called()

    row = _last_briefing()
    assert row is not None
    assert row.canal == "telegram"
    assert row.destinatario == "dona"
    assert row.success is True


def test_dry_run_logs_dry_run_prefix(capfd: pytest.CaptureFixture[str]) -> None:
    import structlog.testing

    tool = _make_tool(dry_run=True)
    with structlog.testing.capture_logs() as cap:
        tool.send_to_dona("msg de teste seca")

    events = [e["event"] for e in cap]
    assert any("[DRY_RUN] Telegram" in e for e in events)


# ── SUCESSO ────────────────────────────────────────────────────────────────

def test_send_success_posts_correct_payload() -> None:
    settings.telegram_bot_token = "FAKE_TOKEN"
    settings.telegram_dona_chat_id = "12345"
    tool = _make_tool(dry_run=False)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"ok": True, "result": {"message_id": 99}}
    mock_resp.raise_for_status = MagicMock()

    with patch("app.tools.telegram.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp

        result = tool.send_to_dona("Mensagem real de teste")

    assert result == "ok"

    call_kwargs = mock_client.post.call_args
    payload = call_kwargs.kwargs["json"]
    assert payload["chat_id"] == "12345"
    assert payload["text"] == "Mensagem real de teste"
    assert payload["parse_mode"] == "Markdown"

    row = _last_briefing()
    assert row is not None
    assert row.success is True
    assert "Mensagem real de teste" in row.conteudo


# ── FALHA ──────────────────────────────────────────────────────────────────

def test_send_failure_records_error() -> None:
    settings.telegram_bot_token = "FAKE_TOKEN"
    settings.telegram_dona_chat_id = "12345"
    tool = _make_tool(dry_run=False)

    http_error = httpx.HTTPStatusError(
        "Bad Request",
        request=MagicMock(),
        response=MagicMock(status_code=400),
    )

    with patch("app.tools.telegram.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = http_error

        result = tool.send_to_dona("Mensagem que vai falhar")

    assert result.startswith("error:")

    row = _last_briefing()
    assert row is not None
    assert row.success is False
