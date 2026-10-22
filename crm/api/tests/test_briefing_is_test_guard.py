"""Blindagem do anti-dobro contra dry-run/teste (incidente briefing 20h).

Causa raiz: rodadas de dry-run/teste gravavam em briefings_sent com
success=True mesmo sem enviar nada, e _already_delivered_today contava
qualquer success=True do dia como entrega real → bloqueava o envio real.

Fix: coluna is_test + helper is_test_or_dryrun() + filtro is_test.is_(False)
no anti-dobro.

Cobre:
  (a) dry-run grava is_test=True
  (b) envio real grava is_test=False (via lógica do helper, sem env de teste)
  (c) _already_delivered_today ignora is_test=True e respeita is_test=False
  (d) o helper detecta os 3 modos de não-entrega
"""
import os
from unittest.mock import MagicMock, patch

from app.config import is_test_or_dryrun, settings
from app.db.session import SessionLocal
from app.db import models


def _last_briefing() -> models.BriefingSent | None:
    with SessionLocal() as session:
        return (
            session.query(models.BriefingSent)
            .order_by(models.BriefingSent.id.desc())
            .first()
        )


# ── helper is_test_or_dryrun ─────────────────────────────────────────────────

def test_helper_true_em_dry_run() -> None:
    with patch.object(settings, "dry_run", True):
        assert is_test_or_dryrun() is True


def test_helper_true_sob_pytest() -> None:
    # pytest sempre seta PYTEST_CURRENT_TEST enquanto roda este teste.
    assert os.environ.get("PYTEST_CURRENT_TEST")
    with patch.object(settings, "dry_run", False):
        assert is_test_or_dryrun() is True


def test_helper_true_com_copiloto_test_mode() -> None:
    """COPILOTO_TEST_MODE marca não-entrega mesmo sem dry_run e sem pytest env."""
    saved = os.environ.pop("PYTEST_CURRENT_TEST", None)
    try:
        with patch.object(settings, "dry_run", False), \
             patch.dict(os.environ, {"COPILOTO_TEST_MODE": "1"}, clear=False):
            assert is_test_or_dryrun() is True
    finally:
        if saved is not None:
            os.environ["PYTEST_CURRENT_TEST"] = saved
        os.environ.pop("COPILOTO_TEST_MODE", None)


def test_helper_false_em_entrega_real() -> None:
    """Sem dry_run, sem pytest, sem COPILOTO_TEST_MODE → entrega real (False)."""
    saved = os.environ.pop("PYTEST_CURRENT_TEST", None)
    os.environ.pop("COPILOTO_TEST_MODE", None)
    try:
        with patch.object(settings, "dry_run", False):
            assert is_test_or_dryrun() is False
    finally:
        if saved is not None:
            os.environ["PYTEST_CURRENT_TEST"] = saved


# ── (a) dry-run grava is_test=True ───────────────────────────────────────────

def test_dry_run_grava_is_test_true() -> None:
    from app.tools.telegram import TelegramTool

    with patch.object(settings, "dry_run", True):
        tool = TelegramTool()
        with patch("app.tools.telegram.httpx.Client") as mock_client:
            result = tool.send_to_dona("mensagem dry-run")
        mock_client.assert_not_called()

    assert result == "ok (dry_run)"
    row = _last_briefing()
    assert row is not None
    assert row.is_test is True


# ── (b) envio "real" (helper False) grava is_test=False ──────────────────────

def test_registro_real_grava_is_test_false() -> None:
    """Quando is_test_or_dryrun() retorna False, _record_briefing grava is_test=False.

    Testamos o gravador diretamente forçando o helper a False (simulando um
    envio real fora do pytest), sem chamar nenhuma API externa.
    """
    from app.tools import telegram

    with patch("app.tools.telegram.is_test_or_dryrun", return_value=False):
        telegram._record_briefing(conteudo="entrega real simulada", success=True)

    row = _last_briefing()
    assert row is not None
    assert row.destinatario == "dona"
    assert row.success is True
    assert row.is_test is False


# ── (c) _already_delivered_today respeita is_test ────────────────────────────

def test_anti_dobro_ignora_is_test_true_respeita_false() -> None:
    """A query do anti-dobro precisa filtrar is_test.is_(False).

    Testamos que a coluna is_test entra no conjunto de filtros aplicados —
    prova que registros de teste não podem satisfazer o anti-dobro.
    """
    from app.agents.agente_assistente import _already_delivered_today

    captured: dict = {}

    class _FakeQuery:
        def filter(self, *args):
            captured["filter_args"] = args
            return self

        def first(self):
            return None

    session = MagicMock()
    session.query.return_value = _FakeQuery()

    assert _already_delivered_today(session, "dona") is False

    # Um dos filtros tem que ser sobre BriefingSent.is_test (comparação is False).
    filtros_str = " ".join(str(a) for a in captured["filter_args"])
    assert "is_test" in filtros_str


def test_anti_dobro_real_db_teste_nao_bloqueia_entrega_real() -> None:
    """Integração leve com o DB real: um registro is_test=True NÃO bloqueia.

    Grava um briefing de teste (is_test=True, success=True) pra um destinatario
    sentinela e confirma que _already_delivered_today não o conta.
    """
    from app.agents.agente_assistente import _already_delivered_today

    destinatario = "sentinela_is_test_guard"
    with SessionLocal() as session:
        session.query(models.BriefingSent).filter(
            models.BriefingSent.destinatario == destinatario
        ).delete()
        session.add(models.BriefingSent(
            destinatario=destinatario, canal="telegram",
            conteudo="registro de teste", success=True, is_test=True,
        ))
        session.commit()

    with SessionLocal() as session:
        # registro is_test=True não conta → False
        assert _already_delivered_today(session, destinatario) is False

    # Agora um registro REAL (is_test=False) deve bloquear
    with SessionLocal() as session:
        session.add(models.BriefingSent(
            destinatario=destinatario, canal="telegram",
            conteudo="registro real", success=True, is_test=False,
        ))
        session.commit()

    with SessionLocal() as session:
        assert _already_delivered_today(session, destinatario) is True

    # cleanup
    with SessionLocal() as session:
        session.query(models.BriefingSent).filter(
            models.BriefingSent.destinatario == destinatario
        ).delete()
        session.commit()
