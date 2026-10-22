"""Testes do briefing matinal — Onda 3a."""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import settings
from app.db.session import SessionLocal
from app.main import app
from app.services.briefing_corretora import (
    CoachingItem,
    DadosBriefing,
    LeadRetomar,
    VisitaSemanal,
    montar_texto,
    coletar_dados,
)

SP_TZ = ZoneInfo("America/Sao_Paulo")


def _viz(dia: str, d: date, hora: str, nome: str, status: str, emoji: str,
         bairro: str | None = None) -> VisitaSemanal:
    return VisitaSemanal(dia_semana=dia, data=d, hora=hora, lead_nome=nome,
                         bairro=bairro, status=status, cor_emoji=emoji)


# ── Templates ────────────────────────────────────────────────────────────────


def test_montar_texto_completo():
    dados = DadosBriefing(
        corretora_nome="Pessoa3",
        ligacoes_semana=87,
        contatos_semana=14,
        tarefas_atrasadas=3,
        visitas_semana=[
            _viz("Seg", date(2026, 5, 12), "10h", "João", "futura", "🟠", "Centro"),
            _viz("Ter", date(2026, 5, 13), "14h", "Pessoa", "compareceu", "🟢"),
        ],
        coaching=[
            CoachingItem(
                lead_nome="Pedro",
                trecho_corretora="te mando umas opções por X mil",
                observacao="Talvez vale entender o que ele busca antes de mandar opção.",
                sugestao="Pedro, antes de mandar opções: o que é prioridade pra você?",
                categoria="falta_qualificacao",
                origem="lead_quente",
            ),
        ],
    )
    t = montar_texto(dados)
    assert "Bom dia, Pessoa12!" in t
    # Bloco de ligações saiu do template (rewrite 3e45f936)
    assert "ligações" not in t and "contatos diferentes" not in t
    assert "3 tarefas atrasadas" in t
    assert "🟠 Seg 10h — João (Centro)" in t
    assert "🟢 Ter 14h — Pessoa" in t and "[compareceu]" in t
    # Coaching removido do briefing matinal em 14/05/2026 a pedido da Dona
    # (corretoras não gostavam de receber feedback de atendimento no WhatsApp
    # — ver comentário em app/delivery/corretora_formatter.py:90-93). A coleta
    # continua alimentando o resumo da Dona às 20h, mas não aparece mais aqui.
    assert "COACHING DA SEMANA" not in t
    assert "Pedro" not in t
    # Persona "assistente" (Onda 2 — wiring persona, commit dd42f046) trocou a
    # saudação/despedida do template antigo.
    assert t.endswith("bom dia bom 💛 me responde se quiser ajustar alguma coisa 🐱")


def test_montar_texto_sem_visitas():
    # Template reescrito em 3e45f936 (ondas 8-10, 14/05/2026): a linha "agenda
    # tranquila — bom momento pra prospectar" saiu; sem visitas, o bloco de
    # agenda é omitido inteiro.
    dados = DadosBriefing("Pessoa5", 50, 8, 0)
    t = montar_texto(dados)
    assert "agenda essa semana" not in t
    assert "tranquila" not in t and "prospectar" not in t


def test_montar_texto_sem_bloco_ligacoes():
    """Bloco de ligações da semana NUNCA aparece no briefing (saiu no rewrite
    do template, 3e45f936; a antiga flag BRIEFING_VERSAO_SIMPLIFICADA foi
    removida como dead code)."""
    dados = DadosBriefing("Pessoa1", 0, 0, 0)
    t = montar_texto(dados)
    assert "registrou ligações" not in t
    assert "ligações" not in t


def test_montar_texto_sem_tarefas_atrasadas():
    dados = DadosBriefing("Pessoa3", 10, 5, 0)
    t = montar_texto(dados)
    assert "tarefa" not in t.lower() or "atraso" not in t.lower()


def test_montar_texto_sem_coaching():
    """Sem observações: bloco COACHING é omitido inteiro (sem mensagem placeholder)."""
    dados = DadosBriefing("Pessoa3", 10, 5, 0, coaching=[])
    t = montar_texto(dados)
    assert "COACHING DA SEMANA" not in t
    assert "sem coaching" not in t.lower()


def test_montar_texto_visita_sem_status():
    dados = DadosBriefing(
        "Pessoa3", 1, 1, 0,
        visitas_semana=[
            _viz("Qua", date(2026, 5, 7), "10h", "Corretor", "sem_status", "🟡"),
        ],
    )
    t = montar_texto(dados)
    assert "🟡" in t
    assert "⚠️ sem status" in t
    # A linha-resumo "N visita(s) passada(s) sem status" saiu no rewrite do
    # template (3e45f936, 14/05/2026); a marcação fica só por visita (⚠️).
    assert "passada(s) sem status" not in t


def test_montar_texto_visitas_passadas_marcadas():
    dados = DadosBriefing(
        "Pessoa3", 1, 1, 0,
        visitas_semana=[
            _viz("Seg", date(2026, 5, 12), "10h", "Futuro", "futura", "🟠"),
            _viz("Dom", date(2026, 5, 11), "9h",  "Foi",    "compareceu", "🟢"),
            _viz("Sáb", date(2026, 5, 10), "15h", "Sumiu",  "no_show", "🔴"),
        ],
    )
    t = montar_texto(dados)
    assert "🟠 Seg 10h — Futuro" in t
    assert "🟢 Dom 9h — Foi [compareceu]" in t
    assert "🔴 Sáb 15h — Sumiu [não compareceu]" in t


# ── Privacidade ──────────────────────────────────────────────────────────────


def test_coletar_dados_filtra_por_corretora_id():
    """Regra de ouro: chamada com corretora_id=1 nunca retorna dados de outras.

    Onda 9 (commit 3e45f936, "ondas 8-10 completas — Postgres-only...",
    2026-05-14) desacoplou `tarefas_atrasadas` do snapshot: agora vem sempre
    de `_coletar_tarefas_atrasadas(kommo_user_id)` (fonte Kommo direto, ver
    comentário em app/services/briefing_corretora.py:581-582), não mais de
    `_coletar_metricas_dia_snapshot`. A mesma leva também renomeou/mudou a
    assinatura de `_coletar_visitas_semana` → `_coletar_visitas_semana_pg`
    (Postgres em vez de prefixo de Google Calendar) e ajustou o `limite` do
    coaching de 5 para 2.
    """
    fake_metricas = {"total_leads_mes": 7, "atendidos_mes": 4,
                     "tarefas_atrasadas": 999, "fontes_falhas": []}
    with patch("app.services.briefing_corretora._coletar_ligacoes_semana_snapshot",
               return_value=(50, 10)) as m_lig, \
         patch("app.services.briefing_corretora._coletar_metricas_dia_snapshot",
               return_value=fake_metricas) as m_met, \
         patch("app.services.briefing_corretora._coletar_tarefas_atrasadas",
               return_value=2) as m_tarefas, \
         patch("app.services.briefing_corretora._coletar_visitas_semana_pg", return_value=[]) as m_vis, \
         patch("app.services.briefing_corretora.coletar_coaching", return_value=[]) as m_coach:
        dados = coletar_dados(corretora_id=1)
    assert dados.corretora_nome == "Pessoa5"  # id=1 é Pessoa5
    assert dados.tarefas_atrasadas == 2
    assert dados.snapshot_disponivel is True
    m_lig.assert_called_once_with(1)
    m_met.assert_called_once_with(1)
    m_tarefas.assert_called_once()
    # REGRA OURO (privacidade): a coleta é SEMPRE escopada na corretora pedida —
    # o briefing de uma nunca pode contar tarefa de outra. Em 31/07 o argumento
    # deixou de ser o kommo_user_id (10000006) e virou o id interno: a fonte saiu
    # do Kommo e passou a ser `tarefas_corretora`, do painel. A trava é a mesma;
    # mudou só por qual identificador ela filtra.
    assert m_tarefas.call_args[0][0] == 1  # id interno da Pessoa5
    m_vis.assert_called_once()
    assert m_vis.call_args[0][0] == 1  # corretora_id da Pessoa5 (REGRA OURO)
    m_coach.assert_called_once_with(1, limite=2)  # filtro por corretora_id=1 (REGRA OURO)


def test_coletar_dados_snapshot_ausente():
    """Sem linha em daily_metrics pra hoje → snapshot_disponivel=False.

    `tarefas_atrasadas` não depende mais do snapshot (Onda 9, ver nota acima)
    — vem sempre de `_coletar_tarefas_atrasadas`, mockada aqui devolvendo None
    pra preservar o caso original (coletor indisponível)."""
    with patch("app.services.briefing_corretora._coletar_ligacoes_semana_snapshot",
               return_value=(0, 0)), \
         patch("app.services.briefing_corretora._coletar_metricas_dia_snapshot",
               return_value=None), \
         patch("app.services.briefing_corretora._coletar_tarefas_atrasadas",
               return_value=None), \
         patch("app.services.briefing_corretora._coletar_visitas_semana_pg", return_value=[]), \
         patch("app.services.briefing_corretora.coletar_coaching", return_value=[]):
        dados = coletar_dados(corretora_id=3)
    assert dados.tarefas_atrasadas is None
    assert dados.snapshot_disponivel is False
    t = montar_texto(dados)
    assert "indisponível" in t.lower()


# ── Lead reformat (FIX 2) ────────────────────────────────────────────────────


def test_lead_reformat_json_ok():
    from app.services.lead_reformat import reformatar_leads
    fake_provider = MagicMock()
    fake_provider.complete.return_value = (
        '{"leads": [{"nome": "João", '
        '"voce_disse": "Você pediu apto 2q.", '
        '"sugestao": "Bora marcar visita sexta?"}]}',
        MagicMock(),
    )
    leads = [LeadRetomar(nome="João", contexto="Cliente pediu apto 2q.",
                         mensagem_sugerida="Marcar visita.")]
    out = reformatar_leads("Pessoa3", leads, provider=fake_provider)
    assert len(out) == 1
    assert out[0]["nome"] == "João"
    assert out[0]["voce_disse"] == "Você pediu apto 2q."
    assert out[0]["sugestao"] == "Bora marcar visita sexta?"


def test_lead_reformat_json_invalido():
    from app.services.lead_reformat import reformatar_leads
    fake_provider = MagicMock()
    fake_provider.complete.return_value = ("lixo sem json", MagicMock())
    leads = [LeadRetomar(nome="Corretor", contexto="Pediu casa 3q.",
                         mensagem_sugerida="Tem opção em Bairro X.")]
    out = reformatar_leads("Pessoa5", leads, provider=fake_provider)
    assert out == [{"nome": "Corretor", "voce_disse": "Pediu casa 3q.",
                    "sugestao": "Tem opção em Bairro X."}]


def test_lead_reformat_llm_excecao_fallback():
    from app.services.lead_reformat import reformatar_leads
    fake_provider = MagicMock()
    fake_provider.complete.side_effect = RuntimeError("timeout")
    leads = [LeadRetomar(nome="Corretor", contexto="Quer alugar.",
                         mensagem_sugerida=None)]
    out = reformatar_leads("Pessoa1", leads, provider=fake_provider)
    assert out == [{"nome": "Corretor", "voce_disse": "Quer alugar.", "sugestao": ""}]


def test_briefing_template_nunca_mostra_coaching():
    """Coaching NUNCA aparece no briefing matinal (WhatsApp), mesmo com itens.

    Até 14/05/2026 este teste verificava o oposto (bloco "COACHING DA SEMANA"
    renderizado com até 3 itens). Nesse dia o coaching foi removido do
    briefing matinal a pedido da Dona — corretoras não gostavam de receber
    feedback de atendimento no WhatsApp (ver comentário em
    app/delivery/corretora_formatter.py:90-93). A coleta (`coletar_coaching`)
    continua alimentando o resumo da Dona às 20h; só sumiu daqui. Reaproveito
    o teste como guarda de regressão: se `coaching` não-vazio vazar de volta
    pro WhatsApp, este teste pega.
    """
    items = [
        CoachingItem(lead_nome="A", trecho_corretora="trecho A",
                     observacao="obs A.", sugestao="sug A.",
                     categoria="resposta_seca", origem="lead_quente"),
        CoachingItem(lead_nome="B", trecho_corretora=None,
                     observacao="obs B.", sugestao=None,
                     categoria="perdeu_deixa", origem="lead_esfriou"),
        CoachingItem(lead_nome="C", trecho_corretora="trecho C",
                     observacao="obs C.", sugestao="sug C.",
                     categoria="outro", origem="lead_quente"),
    ]
    dados = DadosBriefing("Pessoa3", 5, 2, 1, coaching=items)
    t = montar_texto(dados)
    assert "COACHING DA SEMANA" not in t
    assert "obs A." not in t and "obs B." not in t and "obs C." not in t

    # 0 observações → bloco omitido
    dados_vazio = DadosBriefing("Pessoa3", 5, 2, 1, coaching=[])
    t2 = montar_texto(dados_vazio)
    assert "COACHING DA SEMANA" not in t2


# ── Endpoint ─────────────────────────────────────────────────────────────────


@pytest.fixture
def client():
    return TestClient(app)


def _hdrs():
    return {"X-Internal-Token": settings.copiloto_internal_token}


def test_endpoint_briefing_sem_token_401(client):
    r = client.post("/assistente/briefing/test",
                    json={"corretora_id": 3, "destino_phone": "+5500000000000"})
    assert r.status_code == 401


def test_endpoint_briefing_corretora_invalida_404(client):
    r = client.post("/assistente/briefing/test", headers=_hdrs(),
                    json={"corretora_id": 99999, "destino_phone": "+5500000000000"})
    assert r.status_code == 404


def test_endpoint_briefing_phone_invalido_400(client):
    r = client.post("/assistente/briefing/test", headers=_hdrs(),
                    json={"corretora_id": 3, "destino_phone": "abc"})
    assert r.status_code == 400


def test_endpoint_briefing_envia_e_persiste(client):
    fake_dados = DadosBriefing("Pessoa3", 10, 5, 0)
    fake_send = MagicMock()
    with patch("app.services.briefing_corretora.coletar_dados", return_value=fake_dados), \
         patch("app.services.briefing_corretora.EvolutionClient") as MockEvo:
        MockEvo.return_value.send_text = fake_send

        # Limpa antes
        with SessionLocal() as s:
            s.execute(text("DELETE FROM conversations WHERE phone='+5500000000000' AND text LIKE 'Bom dia, Pessoa12%'"))
            s.commit()

        r = client.post(
            "/assistente/briefing/test",
            headers=_hdrs(),
            json={"corretora_id": 3, "destino_phone": "+5500000000000"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "sent"
    assert "Bom dia, Pessoa12" in body["preview"]
    fake_send.assert_called_once()
    call = fake_send.call_args.kwargs
    assert call["instance_name"] == settings.evolution_assistente_instance
    assert call["to_number"] == "+5500000000000"
    assert "Bom dia, Pessoa12" in call["text"]

    with SessionLocal() as s:
        n = s.execute(text(
            "SELECT COUNT(*) FROM conversations WHERE phone='+5500000000000' "
            "AND text LIKE 'Bom dia, Pessoa12%' AND sender_kind='assistant'"
        )).scalar()
    assert n >= 1
