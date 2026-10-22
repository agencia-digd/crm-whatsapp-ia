"""Trava de confirmação estruturada (regressão Onda 1) — Onda 2."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.agents.assistente.agent import (
    AgenteAssistente,
    _tool_marcar_visita,
    _tool_registrar_venda,
)


def test_marcar_visita_sem_confirmacao_retorna_needs_confirmation():
    out = _tool_marcar_visita(evento_id="abc123", status="compareceu", confirmado=False)
    assert out["status"] == "needs_confirmation"
    assert out["action"] == "marcar_visita"
    assert out["params"]["evento_id"] == "abc123"
    assert out["params"]["status"] == "compareceu"
    assert "abc123" in out["preview"]


def test_marcar_visita_com_confirmacao_executa():
    fake = MagicMock(returncode=0, stdout="ok", stderr="")
    with patch("app.agents.assistente.agent.subprocess.run", return_value=fake):
        out = _tool_marcar_visita(evento_id="abc", status="no_show", confirmado=True)
    assert out["status"] == "ok"


def test_marcar_visita_normaliza_status_falado():
    # corretora fala "não compareceu" / "deu bolo" — vira no_show canônico
    out = _tool_marcar_visita(evento_id="abc", status="não compareceu", confirmado=False)
    assert out["status"] == "needs_confirmation"
    assert out["params"]["status"] == "no_show"
    out = _tool_marcar_visita(evento_id="abc", status="deu bolo", confirmado=False)
    assert out["params"]["status"] == "no_show"


def test_marcar_visita_status_invalido_lista_validos():
    out = _tool_marcar_visita(evento_id="abc", status="sumiu", confirmado=False)
    assert out["status"] == "error"
    assert "no_show" in out["message"]


def test_marcar_visita_resolve_por_cliente():
    achado = {"evento_id": "ev999", "lead_nome": "Kleber Silva",
              "data_sp": "08/06 16:45", "corretora": "Pessoa1"}
    with patch("app.agents.assistente.agent._resolver_visita_por_cliente", return_value=achado):
        out = _tool_marcar_visita(cliente="Kleber", status="no_show",
                                  corretora="Pessoa11", confirmado=False)
    assert out["status"] == "needs_confirmation"
    assert out["params"]["evento_id"] == "ev999"
    assert "Kleber Silva" in out["preview"]


def test_marcar_visita_sem_cliente_nem_evento_pede_nome():
    out = _tool_marcar_visita(status="compareceu", confirmado=False)
    assert out["status"] == "error"
    assert "cliente" in out["message"]


def test_registrar_venda_sem_confirmacao_retorna_needs_confirmation():
    out = _tool_registrar_venda(
        corretora="Pessoa11", cliente="Lurdes", valor=300000, mes="2026-05",
        confirmado=False,
    )
    assert out["status"] == "needs_confirmation"
    assert out["action"] == "registrar_venda"
    assert out["params"]["corretora"] == "Pessoa11"
    assert "Lurdes" in out["preview"]


def test_registrar_venda_com_confirmacao_executa():
    fake = MagicMock(returncode=0, stdout="ok", stderr="")
    with patch("app.agents.assistente.agent.subprocess.run", return_value=fake):
        out = _tool_registrar_venda(
            corretora="Pessoa11", cliente="Lurdes", valor=300000,
            mes="2026-05", confirmado=True,
        )
    assert out["status"] == "ok"


def test_dona_chamando_tool_sem_corretora_retorna_needs_corretora_specified():
    """Quando sender é dona e tool de corretora chega sem corretora arg."""
    b = AgenteAssistente(sender_kind="dona", sender_nome="Pessoa Pessoa9 Acme")
    # Simula a LLM chamando registrar_venda sem 'corretora'
    out = b._execute_tool("registrar_venda", {"cliente": "X", "valor": 100, "mes": "2026-05"})
    assert out["status"] == "needs_corretora_specified"


def test_corretora_auto_inject_corretora_no_args():
    """Quando sender é corretora, sender_nome vira corretora arg automaticamente.

    `registrar_ligacao` exige `confirmado=True` pra executar de fato (trava de
    confirmação estruturada — ver docstring de app/agents/assistente/agent.py:12
    e os demais testes deste arquivo, ex. test_marcar_visita_com_confirmacao_executa).
    O teste original não passava `confirmado`, então sempre caía no preview
    `needs_confirmation` — nunca testou de fato se o auto-inject chegava até o
    subprocess. Passar `confirmado=True` preserva o propósito original (auto-fill
    de `corretora`) e de fato exercita o caminho que o assert final verifica.
    """
    b = AgenteAssistente(sender_kind="corretora", sender_id=3, sender_nome="Pessoa3")
    fake = MagicMock(returncode=0, stdout="ok", stderr="")
    with patch("app.agents.assistente.agent.subprocess.run", return_value=fake) as mock_run:
        # Tool registrar_ligacao precisa de corretora; sem args a Assistente auto-fill.
        out = b._execute_tool(
            "registrar_ligacao",
            {"ligacoes": 10, "atendeu": 5, "prosseguiu": 2, "confirmado": True},
        )
    assert out["status"] == "ok"
    cmd = mock_run.call_args[0][0]
    assert "Pessoa3" in cmd
