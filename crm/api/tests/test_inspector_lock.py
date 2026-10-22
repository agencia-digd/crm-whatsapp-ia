"""
Testes do advisory lock por corretora no Inspector (P0 — anti-concorrência).

O Inspector pode ser disparado concorrente pra mesma corretora (timer +
invocação manual, dois crons, retry sobreposto). Sem trava, duplica chamadas
LLM (Classifier/Analyzer) = custo real. `pg_try_advisory_lock` garante que a
2ª execução concorrente PULA a corretora com log claro.

Sem DB real, sem LLM real — tudo mockado.
"""
import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

from app.inspector.pipeline import (
    _try_acquire_corretora_lock,
    _release_corretora_lock,
    run_daily_inspection,
)


_CORRETORA_DB = {
    "id": 3,
    "nome": "Pessoa3",
    "evolution_instance": "acme_corretora_a",
    "kommo_user_id": 10000003,
    "evolution_connected_since": datetime(2026, 4, 1, 0, 0, 0),
}


def _mock_db_session(corretora_row: dict) -> MagicMock:
    mock_session = MagicMock()
    mock_corretora = MagicMock()
    for k, v in corretora_row.items():
        setattr(mock_corretora, k, v)
    mock_query = MagicMock()
    mock_query.filter.return_value = mock_query
    mock_query.all.return_value = [mock_corretora]
    mock_session.query.return_value = mock_query
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    return mock_session


# ── Helpers de lock (unitário) ────────────────────────────────────────────────

class TestTryAcquireLock:
    def test_lock_livre_retorna_conexao(self) -> None:
        """pg_try_advisory_lock=True → retorna a conexão que segura a trava."""
        conn = MagicMock()
        conn.execute.return_value.scalar.return_value = True
        with patch("app.inspector.pipeline.engine.connect", return_value=conn):
            got = _try_acquire_corretora_lock(3)
        assert got is conn
        conn.close.assert_not_called()

    def test_lock_ocupado_retorna_none_e_fecha(self) -> None:
        """pg_try_advisory_lock=False → retorna None e devolve a conexão ao pool."""
        conn = MagicMock()
        conn.execute.return_value.scalar.return_value = False
        with patch("app.inspector.pipeline.engine.connect", return_value=conn):
            got = _try_acquire_corretora_lock(3)
        assert got is None
        conn.close.assert_called_once()

    def test_release_faz_unlock_e_fecha(self) -> None:
        conn = MagicMock()
        _release_corretora_lock(conn, 3)
        # chamou pg_advisory_unlock e fechou
        assert conn.execute.call_count == 1
        conn.close.assert_called_once()

    def test_release_conn_none_noop(self) -> None:
        # não deve levantar
        _release_corretora_lock(None, 3)


# ── Fluxo: 2ª chamada concorrente é pulada ────────────────────────────────────

class TestConcorrenciaPulada:
    def test_segunda_execucao_concorrente_e_pulada(self) -> None:
        """Com o lock OCUPADO (outra sessão rodando), a corretora é pulada:
        nenhuma chamada LLM (classify/analyze), sem run criado, skipped_lock
        populado.
        """
        with (
            patch("app.inspector.pipeline.SessionLocal",
                  return_value=_mock_db_session(_CORRETORA_DB)),
            patch("app.inspector.pipeline.EvolutionTool"),
            # Lock ocupado → simula 2ª execução concorrente
            patch("app.inspector.pipeline._try_acquire_corretora_lock",
                  return_value=None) as mock_lock,
            patch("app.inspector.pipeline.classify_lead") as mock_classify,
            patch("app.inspector.pipeline.analyze_lead") as mock_analyze,
            patch("app.inspector.pipeline._create_inspector_run") as mock_create_run,
            patch("app.inspector.pipeline._mark_orphan_runs"),
        ):
            stats = run_daily_inspection(only_corretora_id=3, dry_run=True)

        # Tentou pegar o lock
        mock_lock.assert_called_once_with(3)
        # NÃO gastou LLM nem criou run — pulou antes de tudo
        mock_classify.assert_not_called()
        mock_analyze.assert_not_called()
        mock_create_run.assert_not_called()
        # Registrou o skip
        assert stats.get("skipped_lock") == [
            {"corretora_id": 3, "corretora": "Pessoa3"}
        ]
        assert stats["total_leads"] == 0

    def test_lock_liberado_no_finally_mesmo_com_erro(self) -> None:
        """Se _process_corretora levanta, o lock é liberado no finally."""
        released: list = []

        def _fake_release(conn, cid):
            released.append(cid)

        with (
            patch("app.inspector.pipeline.SessionLocal",
                  return_value=_mock_db_session(_CORRETORA_DB)),
            patch("app.inspector.pipeline.EvolutionTool"),
            patch("app.inspector.pipeline._try_acquire_corretora_lock",
                  return_value=MagicMock()),
            patch("app.inspector.pipeline._release_corretora_lock",
                  side_effect=_fake_release),
            patch("app.inspector.pipeline._process_corretora",
                  side_effect=RuntimeError("boom")),
        ):
            try:
                run_daily_inspection(only_corretora_id=3, dry_run=True)
            except RuntimeError:
                pass

        # liberou o lock da corretora 3 apesar do erro
        assert released == [3]
