"""
Idempotência POR MENSAGEM do briefing multi-mensagem da Dona (P0).

O briefing rico da Dona sai em N mensagens (Msg 1 métricas + narrativas +
padrão). Num re-run do mesmo dia:
  - 2º run com TUDO já entregue → NÃO reenvia nada (anti-dobro)
  - run anterior que entregou só parte (crash no meio) → reenvia SÓ o que falta
    (não perde narrativa, não dobra a Msg 1)

Sem DB real, sem envio real, sem LLM real — tudo mockado.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.agents.agente_assistente import (
    _already_sent_contents_today,
    run_daily_briefing,
)


# ── _already_sent_contents_today (unitário) ───────────────────────────────────

def test_already_sent_contents_retorna_set_de_conteudos() -> None:
    session = MagicMock()
    session.query.return_value.filter.return_value.all.return_value = [
        ("msg 1",), ("narrativa A",), ("padrao",),
    ]
    got = _already_sent_contents_today(session, "dona")
    assert got == {"msg 1", "narrativa A", "padrao"}


def test_already_sent_contents_vazio_quando_nada_entregue() -> None:
    session = MagicMock()
    session.query.return_value.filter.return_value.all.return_value = []
    assert _already_sent_contents_today(session, "dona") == set()


# ── Fixtures do run ───────────────────────────────────────────────────────────

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


def _run_with(dona_sent_before):
    """Context-manager manual: entra em todos os patches, roda, devolve
    (result, mock_telegram_cls)."""
    import contextlib

    def _fake_sent(session, destinatario):
        return dona_sent_before if destinatario == "dona" else set()

    with contextlib.ExitStack() as stack:
        mock_session_cls = stack.enter_context(
            patch("app.agents.agente_assistente.SessionLocal"))
        stack.enter_context(patch("app.agents.agente_assistente._fetch_today_analyses",
                                  return_value=[_mock_analysis(1)]))
        stack.enter_context(patch(
            "app.agents.agente_assistente._fetch_corretoras_with_evolution",
            return_value=[]))
        stack.enter_context(patch(
            "app.agents.agente_assistente._fetch_internal_phones", return_value=[]))
        stack.enter_context(patch(
            "app.agents.agente_assistente._snapshot_disponivel_hoje",
            return_value=False))
        stack.enter_context(patch(
            "app.agents.agente_assistente.fetch_whatsapp_metrics", return_value={}))
        stack.enter_context(patch(
            "app.agents.agente_assistente._already_sent_contents_today",
            side_effect=_fake_sent))
        stack.enter_context(patch(
            "app.agents.agente_assistente._mark_consumed_dona"))
        stack.enter_context(patch(
            "app.agents.agente_assistente._mark_consumed_pessoa"))
        mock_telegram_cls = stack.enter_context(
            patch("app.agents.agente_assistente.TelegramTool"))
        # `send_to_pessoa_telegram`/`_corretor_config` são importados LOCALMENTE
        # dentro de run_daily_briefing (from app.tools.telegram import ...,
        # ver Onda 7 · Fase 3C, commit 185a4f48) — não existem como atributo
        # do módulo app.agents.agente_assistente, então o patch tem que mirar o
        # módulo de origem (o import local ainda pega a versão patchada,
        # porque a resolução do nome só acontece quando run_daily_briefing
        # roda, dentro do `with`).
        stack.enter_context(patch(
            "app.tools.telegram.send_to_pessoa_telegram", return_value=True))
        stack.enter_context(patch(
            "app.tools.telegram._corretor_config", return_value=None))
        stack.enter_context(patch(
            "app.agents.agente_assistente._notificar_falha_briefing"))

        mock_session = MagicMock()
        mock_session_cls.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_telegram_cls.return_value.send_to_dona.return_value = "ok"

        result = run_daily_briefing(dry_run=False)
        sent_msgs = [
            c.args[0]
            for c in mock_telegram_cls.return_value.send_to_dona.call_args_list
        ]
    return result, sent_msgs


# ── 2º run do dia NÃO reenvia (tudo já entregue) ──────────────────────────────

def test_segundo_run_tudo_entregue_nao_reenvia() -> None:
    """Cenário 1 (dia zerado) gera 1 msg determinística. Se ela já saiu hoje,
    o 2º run NÃO reenvia nada."""
    # Primeiro run: descobre qual é a Msg 1 gerada (nada entregue antes)
    _, sent_first = _run_with(dona_sent_before=set())
    assert len(sent_first) == 1  # cenário dia zerado → 1 msg
    msg1 = sent_first[0]

    # Segundo run: essa msg já está em briefings_sent de hoje → skip total
    result2, sent_second = _run_with(dona_sent_before={msg1})
    assert sent_second == []  # nada reenviado
    assert "skip" in result2["dona_status"]


def test_reenvio_parcial_manda_so_o_que_falta() -> None:
    """Se a Msg 1 já saiu mas o resto não, o run reenvia SÓ o que falta —
    aqui simulamos com uma msg que NÃO bate o conteúdo gerado (então é enviada).
    """
    # Run com um conteúdo aleatório já "entregue" que não corresponde à Msg 1
    # real → nada é filtrado, a Msg 1 é enviada normalmente.
    result, sent = _run_with(dona_sent_before={"conteudo-antigo-diferente"})
    assert len(sent) == 1
    assert result["dona_status"] == "✓"
