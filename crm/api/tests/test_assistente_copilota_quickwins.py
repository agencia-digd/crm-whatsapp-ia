"""Testes dos QUICK WINS da Assistente Copilota (Missão 10 — Revisão Fable).

100% mockados: nada de rede, nada de banco real, nada de subprocess real.
Cobre: parse de ligações, roteador→handlers, visita_status (acha/não acha),
rascunho (texto sem ação), histórico best-effort (guarda anti-teste).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import app.api.assistente_copilota as bc
from app.llm.provider import TokenUsage


def _mock_provider(complete_json: str = "{}", text_out: str = "texto") -> MagicMock:
    prov = MagicMock()
    prov.complete.return_value = (complete_json, TokenUsage(0, 0))
    prov.complete_text.return_value = (text_out, TokenUsage(0, 0))
    return prov


# ─────────────────────────── _parse_ligacoes ───────────────────────────

def test_parse_ligacoes_valido():
    with patch.object(bc, "get_llm_provider_assistente",
                      return_value=_mock_provider('{"ligacoes": 12, "atendeu": 5, "prosseguiu": 2}')):
        r = bc._parse_ligacoes("fiz 12 ligações, 5 atenderam, 2 prosseguiram")
    assert r["valido"] is True
    assert (r["ligacoes"], r["atendeu"], r["prosseguiu"]) == (12, 5, 2)
    assert r["erro"] is None


def test_parse_ligacoes_so_total_zera_resto():
    with patch.object(bc, "get_llm_provider_assistente",
                      return_value=_mock_provider('{"ligacoes": 20, "atendeu": null, "prosseguiu": null}')):
        r = bc._parse_ligacoes("fiz 20 ligações hoje")
    assert r["valido"] is True
    assert (r["ligacoes"], r["atendeu"], r["prosseguiu"]) == (20, 0, 0)


def test_parse_ligacoes_atendeu_maior_que_ligacoes_invalido():
    with patch.object(bc, "get_llm_provider_assistente",
                      return_value=_mock_provider('{"ligacoes": 5, "atendeu": 8, "prosseguiu": 1}')):
        r = bc._parse_ligacoes("5 ligações, 8 atenderam")
    assert r["valido"] is False
    assert "atenderam" in r["erro"]


def test_parse_ligacoes_prosseguiu_maior_que_atendeu_invalido():
    with patch.object(bc, "get_llm_provider_assistente",
                      return_value=_mock_provider('{"ligacoes": 10, "atendeu": 3, "prosseguiu": 5}')):
        r = bc._parse_ligacoes("10 ligações, 3 atenderam, 5 prosseguiram")
    assert r["valido"] is False
    assert "prosseguiram" in r["erro"]


def test_parse_ligacoes_sem_total_invalido():
    with patch.object(bc, "get_llm_provider_assistente",
                      return_value=_mock_provider('{"ligacoes": null, "atendeu": 2, "prosseguiu": 1}')):
        r = bc._parse_ligacoes("2 atenderam")
    assert r["valido"] is False
    assert "total" in r["erro"]


def test_parse_ligacoes_llm_quebra_nao_estoura():
    prov = MagicMock()
    prov.complete.side_effect = RuntimeError("boom")
    with patch.object(bc, "get_llm_provider_assistente", return_value=prov):
        r = bc._parse_ligacoes("qualquer coisa")
    assert r["valido"] is False
    assert r["erro"]


# ─────────────────────────── roteador → ligacoes ───────────────────────────

def test_router_ligacoes_devolve_confirmacao_com_botao():
    viewer = {"corretora_id": 2, "corretora_label": "Pessoa11"}
    prov = _mock_provider('{"intent": "ligacoes", "ligacoes": {"ligacoes": 12, "atendeu": 5, "prosseguiu": 2}}')
    with patch.object(bc, "get_llm_provider_assistente", return_value=prov), \
         patch.object(bc, "_parse_ligacoes", return_value={"ligacoes": 12, "atendeu": 5, "prosseguiu": 2, "valido": True, "erro": None}), \
         patch.object(bc, "_semana_iso_sp", return_value="2026-W27"), \
         patch.object(bc, "_ligacoes_atual", return_value=None):
        out = bc._responder("corretor", viewer, "Pessoa11", "fiz 12 ligações, 5 atenderam, 2 prosseguiram")
    assert out["intent"] == "ligacoes"
    acao = out["acao"]
    assert acao["tipo"] == "ligacoes"
    assert (acao["ligacoes"], acao["atendeu"], acao["prosseguiu"]) == (12, 5, 2)
    assert acao["semana"] == "2026-W27"


def test_router_ligacoes_mostra_valor_anterior_ao_substituir():
    viewer = {"corretora_id": 2, "corretora_label": "Pessoa11"}
    prov = _mock_provider('{"intent": "ligacoes"}')
    with patch.object(bc, "get_llm_provider_assistente", return_value=prov), \
         patch.object(bc, "_parse_ligacoes", return_value={"ligacoes": 30, "atendeu": 10, "prosseguiu": 4, "valido": True, "erro": None}), \
         patch.object(bc, "_semana_iso_sp", return_value="2026-W27"), \
         patch.object(bc, "_ligacoes_atual", return_value={"ligacoes": 20, "atendeu": 8, "prosseguiu": 3}):
        out = bc._responder("corretor", viewer, "Pessoa11", "fiz 30 ligações")
    assert "SUBSTITUIR" in out["resposta"]
    assert "20/8/3" in out["resposta"]


def test_router_ligacoes_admin_bloqueado():
    viewer = {"corretora_id": None, "role": "admin"}
    prov = _mock_provider('{"intent": "ligacoes"}')
    with patch.object(bc, "get_llm_provider_assistente", return_value=prov):
        out = bc._responder("pessoa", viewer, "pessoa", "fiz 10 ligações")
    assert out["intent"] == "ligacoes"
    assert "acao" not in out


def test_router_ligacoes_parse_invalido_orienta():
    viewer = {"corretora_id": 2}
    prov = _mock_provider('{"intent": "ligacoes"}')
    with patch.object(bc, "get_llm_provider_assistente", return_value=prov), \
         patch.object(bc, "_parse_ligacoes", return_value={"valido": False, "erro": "não entendi os números", "ligacoes": None, "atendeu": None, "prosseguiu": None}):
        out = bc._responder("corretor", viewer, "Pessoa11", "liguei bastante")
    assert "acao" not in out
    assert "não entendi" in out["resposta"].lower() or "nao entendi" in out["resposta"].lower()


# ─────────────────────────── roteador → visita_status ───────────────────────────

def test_router_visita_status_acha_devolve_confirmacao():
    viewer = {"corretora_id": 1, "corretora_label": "Pessoa10"}
    prov = _mock_provider('{"intent": "visita_status", "lead_ref": "Elaine", "visita": {"cliente": "Elaine", "status": "compareceu"}}')
    with patch.object(bc, "get_llm_provider_assistente", return_value=prov), \
         patch.object(bc, "_achar_visita_agendada", return_value={"id": 77, "lead_nome": "Elaine Souza", "quando": "02/07 15:00"}):
        out = bc._responder("corretor", viewer, "Pessoa10", "a Elaine compareceu")
    assert out["intent"] == "visita_status"
    acao = out["acao"]
    assert acao["tipo"] == "visita_status"
    assert acao["visita_id"] == 77
    assert acao["status"] == "compareceu"


def test_router_visita_status_no_show():
    viewer = {"corretora_id": 1}
    prov = _mock_provider('{"intent": "visita_status", "visita": {"cliente": "João", "status": "no_show"}}')
    with patch.object(bc, "get_llm_provider_assistente", return_value=prov), \
         patch.object(bc, "_achar_visita_agendada", return_value={"id": 9, "lead_nome": "João", "quando": "01/07 10:00"}):
        out = bc._responder("corretor", viewer, "Pessoa10", "o João furou")
    assert out["acao"]["status"] == "no_show"
    assert out["acao"]["visita_id"] == 9


def test_router_visita_status_nao_acha_lista_agendadas():
    viewer = {"corretora_id": 1}
    prov = _mock_provider('{"intent": "visita_status", "visita": {"cliente": "Fulano", "status": "compareceu"}}')
    with patch.object(bc, "get_llm_provider_assistente", return_value=prov), \
         patch.object(bc, "_achar_visita_agendada", return_value=None), \
         patch.object(bc, "_visitas_agendadas_recentes", return_value=[{"lead_nome": "Corretor", "quando": "03/07 09:00"}]):
        out = bc._responder("corretor", viewer, "Pessoa10", "o Fulano compareceu")
    assert "acao" not in out
    assert "Corretor" in out["resposta"]


def test_router_visita_status_nao_acha_nada():
    viewer = {"corretora_id": 1}
    prov = _mock_provider('{"intent": "visita_status", "visita": {"cliente": "Zé", "status": "compareceu"}}')
    with patch.object(bc, "get_llm_provider_assistente", return_value=prov), \
         patch.object(bc, "_achar_visita_agendada", return_value=None), \
         patch.object(bc, "_visitas_agendadas_recentes", return_value=[]):
        out = bc._responder("corretor", viewer, "Pessoa10", "o Zé compareceu")
    assert "acao" not in out
    assert "Confere o nome" in out["resposta"]


# ─────────────────────────── roteador → rascunho ───────────────────────────

def test_router_rascunho_texto_sem_acao():
    viewer = {"corretora_id": 3, "corretora_label": "Pessoa12"}
    prov = _mock_provider('{"intent": "rascunho", "lead_ref": "João"}', text_out="Oi João!\n\nVi que você curtiu o apto.\n\nBora marcar uma visita?")
    fake_busca = {"leads": [{"lead_id": "k1", "nome": "João Silva"}]}
    fake_conv = {"raiox": {}, "coaching": {}, "mensagens": []}
    with patch.object(bc, "get_llm_provider_assistente", return_value=prov), \
         patch.object(bc, "_get", side_effect=lambda path, user, params=None: fake_busca if "busca" in path else fake_conv):
        out = bc._responder("corretor", viewer, "Pessoa12", "me dá uma mensagem pro João")
    assert out["intent"] == "rascunho"
    assert "acao" not in out  # NUNCA gera ação/envio
    assert "copia e manda" in out["resposta"].lower()
    assert "João Silva" in out["resposta"]


def test_router_rascunho_sem_lead_pede_nome():
    viewer = {"corretora_id": 3}
    prov = _mock_provider('{"intent": "rascunho", "lead_ref": "Fantasma"}')
    with patch.object(bc, "get_llm_provider_assistente", return_value=prov), \
         patch.object(bc, "_get", return_value={"leads": []}):
        out = bc._responder("corretor", viewer, "Pessoa12", "me dá mensagem pro Fantasma")
    assert "acao" not in out
    assert out["intent"] == "rascunho"


# ─────────────────────────── _executar_acao → ligacoes/visita ───────────────────────────

def test_executar_acao_ligacoes_upsert(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "1")  # guarda anti-teste: não roda subprocess
    with patch.object(bc, "_viewer_info", return_value={"corretora_id": 2}):
        r = bc._executar_acao("corretor", {"tipo": "ligacoes", "ligacoes": 12, "atendeu": 5, "prosseguiu": 2, "semana": "2026-W27"})
    assert r["ok"] is True
    assert "12/5/2" in r["msg"]


def test_executar_acao_visita_status(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "1")  # guarda anti-teste: não toca no banco
    with patch.object(bc, "_viewer_info", return_value={"corretora_id": 1}):
        r = bc._executar_acao("corretor", {"tipo": "visita_status", "visita_id": 5, "status": "compareceu"})
    assert r["ok"] is True
    assert "compareceu" in r["msg"]


def test_executar_acao_visita_status_invalido():
    with patch.object(bc, "_viewer_info", return_value={"corretora_id": 1}):
        r = bc._executar_acao("corretor", {"tipo": "visita_status", "visita_id": None, "status": "compareceu"})
    assert r["ok"] is False


def test_executar_acao_ligacoes_admin_bloqueado():
    with patch.object(bc, "_viewer_info", return_value={"corretora_id": None}):
        r = bc._executar_acao("pessoa", {"tipo": "ligacoes", "ligacoes": 1, "atendeu": 0, "prosseguiu": 0})
    assert r["ok"] is False
    assert "corretora" in r["msg"].lower()


def test_executar_acao_desconhecida():
    with patch.object(bc, "_viewer_info", return_value={"corretora_id": 1}):
        r = bc._executar_acao("corretor", {"tipo": "coisa_estranha"})
    assert r["ok"] is False
    assert "desconhecida" in r["msg"].lower()


# ─────────────────────────── _log_conversa best-effort ───────────────────────────

def test_log_conversa_bloqueado_sob_pytest():
    # PYTEST_CURRENT_TEST está setado pelo próprio pytest → não escreve no banco
    called = {"session": False}

    def _boom():
        called["session"] = True
        raise AssertionError("não deveria abrir sessão sob pytest")

    with patch("app.db.session.SessionLocal", side_effect=_boom):
        bc._log_conversa(corretora_id=1, usuario="corretor", intent="lead",
                         pergunta="x", resposta_resumo="y")
    assert called["session"] is False  # guarda anti-teste segurou


def test_log_conversa_nao_estoura_em_erro(monkeypatch):
    # Mesmo forçando execução (sem guarda), erro de banco vira warning, não exception
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with patch("app.db.session.SessionLocal", side_effect=RuntimeError("db down")):
        # não deve levantar
        bc._log_conversa(corretora_id=1, usuario="corretor", intent="lead",
                         pergunta="x", resposta_resumo="y")


# ─────────────────────────── roteador não quebra intents existentes ───────────────────────────

def test_router_intent_lead_ainda_funciona():
    viewer = {"corretora_id": 1, "corretora_label": "Pessoa10"}
    prov = _mock_provider('{"intent": "lead", "lead_ref": "João"}', text_out="O João tá morno.")
    fake_busca = {"leads": [{"lead_id": "k1", "nome": "João"}]}
    fake_conv = {"raiox": {"temperatura": "morno"}, "coaching": {}, "mensagens": []}
    with patch.object(bc, "get_llm_provider_assistente", return_value=prov), \
         patch.object(bc, "_get", side_effect=lambda path, user, params=None: fake_busca if "busca" in path else fake_conv):
        out = bc._responder("corretor", viewer, "Pessoa10", "como tá o João?")
    assert out["intent"] == "lead"
    assert "acao" not in out


def test_router_intent_anotar_ainda_funciona():
    viewer = {"corretora_id": 1}
    prov = _mock_provider('{"intent": "anotar", "lead_ref": "Pessoa"}')
    with patch.object(bc, "get_llm_provider_assistente", return_value=prov), \
         patch.object(bc, "_get", return_value={"leads": [{"lead_id": "k9", "nome": "Pessoa"}]}):
        out = bc._responder("corretor", viewer, "Pessoa10", "liguei pra Pessoa, não atendeu")
    assert out["intent"] == "anotar"
    assert out["acao"]["tipo"] == "anotar"
