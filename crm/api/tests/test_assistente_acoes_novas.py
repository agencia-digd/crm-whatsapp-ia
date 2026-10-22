"""Testes das 3 AÇÕES novas da Assistente Copilota (mover · rascunho · ligação).

100% mockados: nada de rede, nada de banco real. Cobre:
- _resolver_etapa_mover (aliases principal + remarketing)
- roteador → mover (confirmação, destrutivo pede 'confirma', ambíguo pergunta,
  não achou orienta, etapa inválida orienta, admin busca sem escopo)
- roteador → ligacao (confirmação, resultado faltando pergunta, ambíguo)
- rascunho (já existia): continua texto-only, NUNCA gera ação
- _executar_acao mover (reuso do /crm/mover via httpx mockado: ok, 403, noop)
- _executar_acao ligacao (guarda anti-teste + validação de payload)
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import app.api.assistente_copilota as bc
from app.llm.provider import TokenUsage


def _mock_provider(complete_json: str = "{}", text_out: str = "texto") -> MagicMock:
    prov = MagicMock()
    prov.complete.return_value = (complete_json, TokenUsage(0, 0))
    prov.complete_text.return_value = (text_out, TokenUsage(0, 0))
    return prov


# ─────────────────────────── _resolver_etapa_mover ───────────────────────────

def test_resolver_etapa_principal_canonicas_e_aliases():
    assert bc._resolver_etapa_mover("Follow-up") == ("principal", "Follow-up")
    assert bc._resolver_etapa_mover("follow up") == ("principal", "Follow-up")
    assert bc._resolver_etapa_mover("venda ganha") == ("principal", "Venda ganha")
    assert bc._resolver_etapa_mover("negociação") == ("principal", "Proposta")
    assert bc._resolver_etapa_mover("visita agendada") == ("principal", "Visita marcada")


def test_resolver_etapa_remarketing():
    assert bc._resolver_etapa_mover("30 dias") == ("remarketing", "30 dias")
    assert bc._resolver_etapa_mover("remarketing 90") == ("remarketing", "90 dias")
    assert bc._resolver_etapa_mover("base do remarketing") == ("remarketing", "Base")


def test_resolver_etapa_invalida():
    assert bc._resolver_etapa_mover("etapa que não existe") is None
    assert bc._resolver_etapa_mover("") is None
    assert bc._resolver_etapa_mover(None) is None


# ─────────────────────────── roteador → mover ───────────────────────────

def _router_mover(cliente: str = "João", etapa: str = "Follow-up") -> MagicMock:
    return _mock_provider(
        '{"intent": "mover", "lead_ref": "%s", "mover": {"cliente": "%s", "etapa": "%s"}}'
        % (cliente, cliente, etapa)
    )


def test_router_mover_devolve_confirmacao():
    viewer = {"corretora_id": 1, "corretora_label": "Pessoa10"}
    achado = [{"id": 42, "nome": "João Silva", "etapa": "Novo"}]
    with patch.object(bc, "get_llm_provider_assistente", return_value=_router_mover()), \
         patch.object(bc, "_leads_spine_por_nome", return_value=achado) as busca:
        out = bc._responder("corretor", viewer, "Pessoa10", "move o João pro Follow-up")
    assert out["intent"] == "mover"
    acao = out["acao"]
    assert acao["tipo"] == "mover"
    assert acao["lead_id"] == 42
    assert acao["stage_destino"] == "Follow-up"
    assert acao["funil"] == "principal"
    assert acao["destrutivo"] is False
    busca.assert_called_once_with("João", 1)  # escopado pela corretora


def test_router_mover_venda_ganha_pede_confirmacao_destrutiva():
    viewer = {"corretora_id": 2}
    achado = [{"id": 7, "nome": "Pessoa Souza", "etapa": "Proposta"}]
    with patch.object(bc, "get_llm_provider_assistente",
                      return_value=_router_mover("Pessoa", "Venda ganha")), \
         patch.object(bc, "_leads_spine_por_nome", return_value=achado):
        out = bc._responder("corretor", viewer, "Pessoa11", "coloca a Pessoa em Venda ganha")
    assert out["acao"]["destrutivo"] is True
    assert out["acao"]["stage_destino"] == "Venda ganha"
    assert "confirma" in out["resposta"].lower()


def test_router_mover_venda_perdida_tambem_destrutivo():
    viewer = {"corretora_id": 2}
    achado = [{"id": 8, "nome": "Pedro Lima", "etapa": "Follow-up"}]
    with patch.object(bc, "get_llm_provider_assistente",
                      return_value=_router_mover("Pedro", "Venda perdida")), \
         patch.object(bc, "_leads_spine_por_nome", return_value=achado):
        out = bc._responder("corretor", viewer, "Pessoa11", "o Pedro desistiu, joga em venda perdida")
    assert out["acao"]["destrutivo"] is True
    assert "confirma" in out["resposta"].lower()


def test_router_mover_ambiguo_pergunta_sem_acao():
    viewer = {"corretora_id": 1}
    achados = [
        {"id": 1, "nome": "João Silva", "etapa": "Novo"},
        {"id": 2, "nome": "João Pedro", "etapa": "Follow-up"},
    ]
    with patch.object(bc, "get_llm_provider_assistente", return_value=_router_mover()), \
         patch.object(bc, "_leads_spine_por_nome", return_value=achados):
        out = bc._responder("corretor", viewer, "Pessoa10", "move o João pro Follow-up")
    assert "acao" not in out
    assert "João Silva" in out["resposta"] and "João Pedro" in out["resposta"]


def test_router_mover_ambiguo_match_exato_resolve():
    viewer = {"corretora_id": 1}
    achados = [
        {"id": 1, "nome": "João Silva", "etapa": "Novo"},
        {"id": 2, "nome": "João", "etapa": "Follow-up"},
    ]
    with patch.object(bc, "get_llm_provider_assistente", return_value=_router_mover("João")), \
         patch.object(bc, "_leads_spine_por_nome", return_value=achados):
        out = bc._responder("corretor", viewer, "Pessoa10", "move o João pra follow-up")
    assert out["acao"]["lead_id"] == 2  # nome citado casa por inteiro


def test_router_mover_nao_achou_orienta():
    viewer = {"corretora_id": 1}
    with patch.object(bc, "get_llm_provider_assistente", return_value=_router_mover("Fantasma")), \
         patch.object(bc, "_leads_spine_por_nome", return_value=[]):
        out = bc._responder("corretor", viewer, "Pessoa10", "move o Fantasma pro Follow-up")
    assert "acao" not in out
    assert "não achei" in out["resposta"].lower() or "nao achei" in out["resposta"].lower()


def test_router_mover_etapa_invalida_lista_etapas():
    viewer = {"corretora_id": 1}
    with patch.object(bc, "get_llm_provider_assistente",
                      return_value=_router_mover("João", "Limbo")):
        out = bc._responder("corretor", viewer, "Pessoa10", "move o João pro Limbo")
    assert "acao" not in out
    assert "Follow-up" in out["resposta"]  # lista as etapas válidas


def test_router_mover_admin_busca_sem_escopo():
    viewer = {"corretora_id": None, "role": "admin"}
    achado = [{"id": 9, "nome": "Corretor Costa", "etapa": "Novo"}]
    with patch.object(bc, "get_llm_provider_assistente", return_value=_router_mover("Corretor")), \
         patch.object(bc, "_leads_spine_por_nome", return_value=achado) as busca:
        out = bc._responder("pessoa", viewer, "pessoa", "move a Corretor pro Follow-up")
    assert out["acao"]["lead_id"] == 9
    busca.assert_called_once_with("Corretor", None)  # admin: sem filtro de corretora


# ─────────────────────────── roteador → ligacao ───────────────────────────

def test_router_ligacao_devolve_confirmacao():
    viewer = {"corretora_id": 1}
    prov = _mock_provider('{"intent": "ligacao", "lead_ref": "Pessoa8", '
                          '"ligacao": {"cliente": "Pessoa8", "resultado": "nao_atendeu", "nota": null}}')
    achado = [{"id": 11, "nome": "Pessoa8 Reis", "etapa": "Follow-up"}]
    with patch.object(bc, "get_llm_provider_assistente", return_value=prov), \
         patch.object(bc, "_leads_spine_por_nome", return_value=achado):
        out = bc._responder("corretor", viewer, "Pessoa10", "liguei pra Pessoa8, não atendeu")
    assert out["intent"] == "ligacao"
    acao = out["acao"]
    assert acao["tipo"] == "ligacao"
    assert acao["lead_id"] == 11
    assert acao["resultado"] == "nao_atendeu"


def test_router_ligacao_com_nota():
    viewer = {"corretora_id": 2}
    prov = _mock_provider('{"intent": "ligacao", '
                          '"ligacao": {"cliente": "Pedro", "resultado": "atendeu", "nota": "vai pensar"}}')
    achado = [{"id": 12, "nome": "Pedro Alves", "etapa": "Em atendimento"}]
    with patch.object(bc, "get_llm_provider_assistente", return_value=prov), \
         patch.object(bc, "_leads_spine_por_nome", return_value=achado):
        out = bc._responder("corretor", viewer, "Pessoa11", "falei com o Pedro, vai pensar")
    assert out["acao"]["resultado"] == "atendeu"
    assert out["acao"]["nota"] == "vai pensar"
    assert "vai pensar" in out["acao"]["resumo"]


def test_router_ligacao_sem_resultado_pergunta():
    viewer = {"corretora_id": 1}
    prov = _mock_provider('{"intent": "ligacao", "ligacao": {"cliente": "Pessoa8", "resultado": null}}')
    with patch.object(bc, "get_llm_provider_assistente", return_value=prov):
        out = bc._responder("corretor", viewer, "Pessoa10", "liguei pra Pessoa8")
    assert "acao" not in out
    assert "atendeu" in out["resposta"].lower()


def test_router_ligacao_ambigua_pergunta():
    viewer = {"corretora_id": 1}
    prov = _mock_provider('{"intent": "ligacao", '
                          '"ligacao": {"cliente": "Corretor", "resultado": "caixa_postal"}}')
    achados = [
        {"id": 1, "nome": "Corretor Costa", "etapa": None},
        {"id": 2, "nome": "Corretor Paula", "etapa": "Novo"},
    ]
    with patch.object(bc, "get_llm_provider_assistente", return_value=prov), \
         patch.object(bc, "_leads_spine_por_nome", return_value=achados):
        out = bc._responder("corretor", viewer, "Pessoa10", "liguei pra Corretor, caixa postal")
    assert "acao" not in out
    assert "Corretor Costa" in out["resposta"] and "Corretor Paula" in out["resposta"]


# ─────────────────────────── rascunho (já existia — não regride) ─────────────

def test_rascunho_frase_nova_texto_sem_acao_nunca_envia():
    viewer = {"corretora_id": 3, "corretora_label": "Pessoa12"}
    prov = _mock_provider('{"intent": "rascunho", "lead_ref": "Fernanda"}',
                          text_out="Oi Fernanda!\n\nVi teu interesse no apto.\n\nBora conversar?")
    fake_busca = {"leads": [{"lead_id": "k9", "nome": "Fernanda Dias"}]}
    fake_conv = {"raiox": {}, "coaching": {}, "mensagens": []}
    with patch.object(bc, "get_llm_provider_assistente", return_value=prov), \
         patch.object(bc, "_get", side_effect=lambda path, user, params=None:
                      fake_busca if "busca" in path else fake_conv):
        out = bc._responder("corretor", viewer, "Pessoa12", "rascunha uma resposta pra Fernanda")
    assert out["intent"] == "rascunho"
    assert "acao" not in out  # rascunho NUNCA vira ação/envio — só texto no chat
    assert "Fernanda Dias" in out["resposta"]
    assert "Oi Fernanda!" in out["resposta"]


# ─────────────────────────── _executar_acao → mover ───────────────────────────

def _payload_mover() -> dict:
    return {"tipo": "mover", "lead_id": 42, "lead_nome": "João Silva",
            "stage_destino": "Follow-up", "funil": "principal"}


def test_executar_acao_mover_guarda_pytest(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "1")
    r = bc._executar_acao("corretor", _payload_mover())
    assert r["ok"] is True
    assert "(pytest)" in r["msg"]


def _resp(status: int, body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body
    return resp


def _client_com(resp: MagicMock) -> MagicMock:
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.post.return_value = resp
    return client


def test_mover_lead_reusa_crm_mover(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    client = _client_com(_resp(200, {"ok": True, "cadencia_ligada": True}))
    with patch.object(bc.httpx, "Client", return_value=client):
        r = bc._mover_lead("corretor", _payload_mover())
    assert r["ok"] is True
    assert "Follow-up" in r["msg"]
    assert "cadência" in r["msg"]  # enroll do Follow-up veio do endpoint reusado
    url = client.post.call_args[0][0]
    assert url.endswith("/crm/mover")
    assert client.post.call_args.kwargs["headers"] == {"X-Painel-User": "corretor"}
    assert client.post.call_args.kwargs["json"] == {
        "lead_id": 42, "stage_destino": "Follow-up", "funil": "principal"}


def test_mover_lead_403_lead_de_outra(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    client = _client_com(_resp(403, {}))
    with patch.object(bc.httpx, "Client", return_value=client):
        r = bc._mover_lead("corretor", _payload_mover())
    assert r["ok"] is False
    assert "🔒" in r["msg"]


def test_mover_lead_noop(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    client = _client_com(_resp(200, {"ok": True, "noop": True}))
    with patch.object(bc.httpx, "Client", return_value=client):
        r = bc._mover_lead("corretor", _payload_mover())
    assert r["ok"] is True
    assert "já tava" in r["msg"]


def test_mover_lead_payload_incompleto():
    r = bc._mover_lead("corretor", {"tipo": "mover", "lead_id": None, "stage_destino": ""})
    assert r["ok"] is False


# ─────────────────────────── _executar_acao → ligacao ───────────────────────────

def test_executar_acao_ligacao_guarda_pytest(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "1")
    r = bc._executar_acao("corretor", {
        "tipo": "ligacao", "lead_id": 11, "lead_nome": "Pessoa8 Reis",
        "resultado": "nao_atendeu", "nota": ""})
    assert r["ok"] is True
    assert "(pytest)" in r["msg"]
    assert "não atendeu" in r["msg"]


def test_executar_acao_ligacao_resultado_invalido():
    r = bc._executar_acao("corretor", {"tipo": "ligacao", "lead_id": 11, "resultado": "sei-la"})
    assert r["ok"] is False


def test_executar_acao_ligacao_sem_lead():
    r = bc._executar_acao("corretor", {"tipo": "ligacao", "resultado": "atendeu"})
    assert r["ok"] is False
