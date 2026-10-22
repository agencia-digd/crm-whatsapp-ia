"""
Testes dos fixes P0 da Revisão Fable etapa 4 (gaps E1 e E2).

E1 — Falha no envio das 20h: avisar + não dobrar
  - anti-dobro: se já entregue com sucesso HOJE, não reenvia (skip)
  - notificação de falha: chamada só quando o envio falha
  - send_failed propagado no retorno (pra exit != 0 no runner)

E2 — Janela ancorada:
  - _fetch_today_analyses usa janela de 36h + consumed_by_dona=false
  - análise de ontem 23h (consumed=false) ENTRA; de 3 dias atrás NÃO

Sem DB real, sem envio real, sem LLM real — tudo mockado.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# E2 — Janela de 36h
# ─────────────────────────────────────────────────────────────────────────────

def test_fetch_window_usa_36h_e_gate_consumed() -> None:
    """A query deve filtrar por analyzed_at >= now-36h E consumed_by_dona=false."""
    from app.agents.agente_assistente import _fetch_today_analyses, _ANALYSES_WINDOW_HOURS

    assert _ANALYSES_WINDOW_HOURS == 36

    captured: dict = {}

    sentinel = MagicMock()
    sentinel.lead_id = "lead-1"

    class _FakeQuery:
        def filter(self, *args):
            captured["filter_args"] = args
            return self

        def order_by(self, *args):  # dedup por lead ordena analyzed_at desc
            return self

        def all(self):
            return [sentinel]

    session = MagicMock()
    session.query.return_value = _FakeQuery()

    antes = datetime.now(timezone.utc)
    result = _fetch_today_analyses(session)
    depois = datetime.now(timezone.utc)

    assert result == [sentinel]
    # Deve haver 2 cláusulas de filtro (janela temporal + consumed_by_dona)
    assert len(captured["filter_args"]) == 2


def _analyzed_at_hours_ago(hours: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def test_janela_ontem_23h_entra_3dias_nao() -> None:
    """Simula o corte da janela: ontem 23h (não consumido) entra; 3 dias atrás sai.

    Testa a semântica temporal do corte (< 36h vs > 36h) de forma pura,
    sem depender do ORM.
    """
    from app.agents.agente_assistente import _ANALYSES_WINDOW_HOURS

    window_start = datetime.now(timezone.utc) - timedelta(hours=_ANALYSES_WINDOW_HOURS)

    ontem_23h = _analyzed_at_hours_ago(20)     # ~ontem à noite, dentro da janela
    tres_dias = _analyzed_at_hours_ago(72)     # muito antiga

    assert ontem_23h >= window_start           # ENTRA
    assert tres_dias < window_start            # NÃO entra


# ─────────────────────────────────────────────────────────────────────────────
# E1 — anti-dobro
# ─────────────────────────────────────────────────────────────────────────────

def test_already_delivered_today_true_quando_ha_sucesso_hoje() -> None:
    """_already_delivered_today retorna True se existe envio success=true hoje."""
    from app.agents.agente_assistente import _already_delivered_today

    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = (123,)

    assert _already_delivered_today(session, "dona") is True


def test_already_delivered_today_false_quando_nao_ha() -> None:
    from app.agents.agente_assistente import _already_delivered_today

    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = None

    assert _already_delivered_today(session, "dona") is False


def _mock_analysis(id: int = 1) -> MagicMock:
    a = MagicMock()
    a.id = id
    a.temperatura = "frio"
    a.status = "nao_atendido"
    a.has_gap = False
    a.analyzed_at = datetime.now(timezone.utc)
    a.corretora_id = 1
    a.raw_analysis = {
        "lead_name": "Lead", "corretora": "Pessoa10",
        "analysis_mode": "sem_atividade",
        "gap_identificado": None, "abordagem_sugerida": None,
        "tarefas_kommo_atrasadas": [], "ultima_msg_de": None, "ultima_msg_at": None,
    }
    return a


class _ContemTudo:
    """Fake de `set` que sempre responde True a `in` — simula "toda mensagem já
    foi entregue hoje" sem precisar prever o texto exato de msg1."""

    def __contains__(self, item: object) -> bool:
        return True


def test_anti_dobro_skipa_envio_dona_ja_entregue() -> None:
    """Se já entregue hoje pra Dona, NÃO chama send_to_dona (só marca consumed).

    P0 (idempotência multi-mensagem, ver docstring de `_already_sent_contents_today`
    em app/agents/agente_assistente.py) substituiu o gate binário
    `_already_delivered_today` por um gate POR MENSAGEM
    (`_already_sent_contents_today`) na decisão de enviar/pular — mockar
    `_already_delivered_today` não tem mais nenhum efeito nesse fluxo.
    """
    from app.agents.agente_assistente import run_daily_briefing

    with patch("app.agents.agente_assistente.SessionLocal") as mock_session_cls, \
         patch("app.agents.agente_assistente._fetch_today_analyses",
               return_value=[_mock_analysis(1)]), \
         patch("app.agents.agente_assistente._fetch_corretoras_with_evolution", return_value=[]), \
         patch("app.agents.agente_assistente._snapshot_disponivel_hoje", return_value=False), \
         patch("app.agents.agente_assistente.fetch_whatsapp_metrics", return_value={}), \
         patch("app.agents.agente_assistente._already_sent_contents_today",
               return_value=_ContemTudo()), \
         patch("app.agents.agente_assistente._mark_consumed_dona") as mock_mark, \
         patch("app.agents.agente_assistente.TelegramTool") as mock_telegram_cls, \
         patch("app.agents.agente_assistente._notificar_falha_briefing") as mock_notify:

        mock_session = MagicMock()
        mock_session_cls.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)

        result = run_daily_briefing(dry_run=False)

    # NÃO enviou
    mock_telegram_cls.return_value.send_to_dona.assert_not_called()
    # Marcou consumed mesmo assim (idempotência de banco)
    mock_mark.assert_called_once()
    # Não é falha → não notifica
    mock_notify.assert_not_called()
    assert result["send_failed"] is False
    assert "skip" in result["dona_status"]


# ─────────────────────────────────────────────────────────────────────────────
# E1 — notificação de falha
# ─────────────────────────────────────────────────────────────────────────────

def test_notifica_falha_quando_telegram_falha() -> None:
    """Se send_to_dona retorna erro, notifica falha e marca send_failed=True."""
    from app.agents.agente_assistente import run_daily_briefing

    with patch("app.agents.agente_assistente.SessionLocal") as mock_session_cls, \
         patch("app.agents.agente_assistente._fetch_today_analyses",
               return_value=[_mock_analysis(1)]), \
         patch("app.agents.agente_assistente._fetch_corretoras_with_evolution", return_value=[]), \
         patch("app.agents.agente_assistente.fetch_whatsapp_metrics", return_value={}), \
         patch("app.agents.agente_assistente._already_delivered_today", return_value=False), \
         patch("app.config.settings.pessoa_whatsapp", ""), \
         patch("app.agents.agente_assistente._mark_consumed_dona") as mock_mark, \
         patch("app.agents.agente_assistente.TelegramTool") as mock_telegram_cls, \
         patch("app.agents.agente_assistente._notificar_falha_briefing") as mock_notify:

        mock_session = MagicMock()
        mock_session_cls.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_telegram_cls.return_value.send_to_dona.return_value = "error: boom"

        result = run_daily_briefing(dry_run=False)

    # Notificou a Dona da falha
    mock_notify.assert_called_once()
    assert mock_notify.call_args[0][0] == "Dona"
    # Não marcou consumed (envio falhou)
    mock_mark.assert_not_called()
    assert result["send_failed"] is True


def test_nao_notifica_quando_envio_ok() -> None:
    """Sucesso no envio → nenhuma notificação de falha, send_failed=False."""
    from app.agents.agente_assistente import run_daily_briefing

    with patch("app.agents.agente_assistente.SessionLocal") as mock_session_cls, \
         patch("app.agents.agente_assistente._fetch_today_analyses",
               return_value=[_mock_analysis(1)]), \
         patch("app.agents.agente_assistente._fetch_corretoras_with_evolution", return_value=[]), \
         patch("app.agents.agente_assistente.fetch_whatsapp_metrics", return_value={}), \
         patch("app.agents.agente_assistente._already_delivered_today", return_value=False), \
         patch("app.config.settings.pessoa_whatsapp", ""), \
         patch("app.agents.agente_assistente._mark_consumed_dona"), \
         patch("app.agents.agente_assistente.TelegramTool") as mock_telegram_cls, \
         patch("app.agents.agente_assistente._notificar_falha_briefing") as mock_notify:

        mock_session = MagicMock()
        mock_session_cls.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_telegram_cls.return_value.send_to_dona.return_value = "ok"

        result = run_daily_briefing(dry_run=False)

    mock_notify.assert_not_called()
    assert result["send_failed"] is False


# ─────────────────────────────────────────────────────────────────────────────
# E1 — notify helper (best-effort, não derruba)
# ─────────────────────────────────────────────────────────────────────────────

def test_notify_helper_sem_token_retorna_false() -> None:
    """Sem token resolvido, notify_briefing_failure retorna False sem levantar."""
    from app.tools import notify

    with patch.object(notify, "_resolve_token", return_value=""):
        assert notify.notify_briefing_failure("Dona", "erro X") is False


def test_notify_helper_posta_payload_correto() -> None:
    """Com token, faz POST com Bearer + text esperado e retorna True em 2xx."""
    from app.tools import notify

    fake_resp = MagicMock()
    fake_resp.status_code = 200

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            _FakeClient.captured = {"url": url, "headers": headers, "json": json}
            return fake_resp

    with patch.object(notify, "_resolve_token", return_value="TESTTOKEN"), \
         patch.object(notify.httpx, "Client", return_value=_FakeClient()):
        ok = notify.notify_briefing_failure("Pessoa", "Timeout")

    assert ok is True
    cap = _FakeClient.captured
    assert cap["headers"]["Authorization"] == "Bearer TESTTOKEN"
    assert "FALHOU pra Pessoa" in cap["json"]["text"]
    assert "Timeout" in cap["json"]["text"]
    assert cap["json"]["task_id"].startswith("copiloto-briefing-fail-pessoa-")


def test_notify_helper_bridge_offline_nao_levanta() -> None:
    """Se o POST levanta (bridge offline), retorna False sem propagar exceção."""
    from app.tools import notify

    def _boom(*a, **k):
        raise ConnectionError("bridge down")

    with patch.object(notify, "_resolve_token", return_value="TESTTOKEN"), \
         patch.object(notify.httpx, "Client", side_effect=_boom):
        assert notify.notify_briefing_failure("Dona", "erro") is False
