"""
Testes unitários do Agente Dona.

Sem acesso a DB real: mocks de SessionLocal e _fetch_today_analyses (nos testes
de run_daily_briefing). Sem envio real: mock de TelegramTool. Sem LLM real:
mock de get_llm_provider.

fetch_whatsapp_metrics() é Postgres-only desde o commit 3e45f936 ("ondas 8-10
completas — Postgres-only...", 2026-05-14): ela lê `mensagens_whatsapp`
direto via SQL, não chama mais `EvolutionTool.get_conversation`/
`list_recent_chats`. Os testes dessa função semeiam linhas reais em
`mensagens_whatsapp` (ver `_seed_mensagens_whatsapp`/`_cleanup_mensagens_whatsapp`
abaixo) — só o check de `instance_status` (`EvolutionTool._client.get_instance_status`)
ainda é mockado, porque essa é a única chamada Evolution que sobrou na função.

Cobertura:
  - build_dona_context() — função pura, testada sem mocks
  - fetch_whatsapp_metrics() — lógica de contagem via Postgres real
  - _is_dia_fraco() / _format_briefing_mini()
  - generate_lead_narrative() / _format_briefing_rich() — verifica chamada ao LLM
  - _build_narratives_messages() — split de mensagens longas
  - run_daily_briefing() — dry_run=True e False
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text as sql_text

from app.db.session import SessionLocal


# ── Helpers ───────────────────────────────────────────────────────────────────

def _seed_mensagens_whatsapp(instance: str, msgs: list[dict]) -> list[str]:
    """Insere linhas sintéticas em `mensagens_whatsapp` pra exercitar a query
    real de fetch_whatsapp_metrics (Postgres-only, ver docstring do módulo).

    Cada item de `msgs`: {"from": "lead"|"corretora", "remote_jid": str,
    "at": iso str, "contato_telefone": str opcional (default: parte local do
    remote_jid), "text": str opcional}.

    Retorna os evolution_message_id inseridos, pra limpar no finally do teste.
    """
    ids: list[str] = []
    with SessionLocal() as s:
        for m in msgs:
            mid = f"TEST-fwm-{uuid.uuid4()}"
            ids.append(mid)
            tel = m.get("contato_telefone", m["remote_jid"].split("@", 1)[0])
            s.execute(
                sql_text(
                    "INSERT INTO mensagens_whatsapp "
                    "(evolution_message_id, instance, remote_jid, contato_telefone, "
                    " from_corretora, tipo_mensagem, conteudo_texto, timestamp_evolution) "
                    "VALUES (:mid, :inst, :jid, :tel, :fc, 'text', :txt, :ts)"
                ),
                {
                    "mid": mid, "inst": instance, "jid": m["remote_jid"], "tel": tel,
                    "fc": m["from"] == "corretora", "txt": m.get("text", ""),
                    "ts": m["at"],
                },
            )
        s.commit()
    return ids


def _cleanup_mensagens_whatsapp(msg_ids: list[str]) -> None:
    if not msg_ids:
        return
    with SessionLocal() as s:
        s.execute(
            sql_text("DELETE FROM mensagens_whatsapp WHERE evolution_message_id = ANY(:ids)"),
            {"ids": msg_ids},
        )
        s.commit()


def _seed_interno(telefone_normalizado: str, nome: str = "Interno Teste") -> None:
    """Semeia um telefone em internos_acme — hoje é a ÚNICA forma real de
    excluir um contato das métricas (fetch_whatsapp_metrics filtra via SQL
    contra essa tabela; o parâmetro `extra_internal_phones` está deprecated —
    ver docstring de fetch_whatsapp_metrics)."""
    with SessionLocal() as s:
        s.execute(
            sql_text(
                "INSERT INTO internos_acme (nome, telefone_normalizado, papel, ativa) "
                "VALUES (:n, :t, 'coordenacao', true) "
                "ON CONFLICT (telefone_normalizado) DO NOTHING"
            ),
            {"n": nome, "t": telefone_normalizado},
        )
        s.commit()


def _cleanup_interno(telefone_normalizado: str) -> None:
    with SessionLocal() as s:
        s.execute(
            sql_text("DELETE FROM internos_acme WHERE telefone_normalizado = :t"),
            {"t": telefone_normalizado},
        )
        s.commit()

def _mock_analysis(
    id: int = 1,
    temperatura: str = "frio",
    status: str = "nao_atendido",
    has_gap: bool = False,
    analysis_mode: str = "sem_atividade",
    corretora: str = "Pessoa10",
    corretora_id: int = 1,
    gap: str | None = None,
    sugestao: str | None = None,
    lead_name: str = "Lead Test",
    analyzed_at: datetime | None = None,
    ultima_msg_de: str | None = None,
    ultima_msg_at: str | None = None,
) -> MagicMock:
    a = MagicMock()
    a.id = id
    a.temperatura = temperatura
    a.status = status
    a.has_gap = has_gap
    a.analyzed_at = analyzed_at or datetime.now(timezone.utc)
    a.corretora_id = corretora_id
    a.raw_analysis = {
        "lead_name": lead_name,
        "corretora": corretora,
        "analysis_mode": analysis_mode,
        "gap_identificado": gap,
        "abordagem_sugerida": sugestao,
        "tarefas_kommo_atrasadas": [],
        "ultima_msg_de": ultima_msg_de,
        "ultima_msg_at": ultima_msg_at,
    }
    return a


def _mock_whatsapp_metrics(
    nome: str,
    enviadas: int = 10,
    recebidas: int = 5,
    contatos: int = 8,
    responderam: int = 5,
    taxa: int = 63,
) -> dict:
    return {
        nome: {
            "msgs_enviadas": enviadas,
            "msgs_recebidas": recebidas,
            "contatos_unicos": contatos,
            "leads_responderam": responderam,
            "taxa_resposta_pct": taxa,
        }
    }


# ── build_dona_context ────────────────────────────────────────────────────────

def test_build_context_dia_vazio() -> None:
    """Nenhuma análise hoje → contexto zerado mas com estrutura completa."""
    from app.agents.agente_assistente import build_dona_context

    ctx = build_dona_context([])

    totais = ctx["totais_globais"]
    assert totais["leads_quentes"] == 0
    assert totais["leads_mornos_com_gap"] == 0
    assert totais["msgs_enviadas_total"] == 0
    assert totais["msgs_recebidas_total"] == 0
    assert totais["contatos_unicos_total"] == 0
    assert ctx["performance_por_corretora"] == []
    assert ctx["top_5_leads_detalhados"] == []
    assert ctx["alertas_pipeline"] == []
    assert "data" in ctx


def test_build_context_com_quentes() -> None:
    """Leads quentes devem aparecer no top_5 antes de mornos; frios não entram."""
    from app.agents.agente_assistente import build_dona_context

    quente = _mock_analysis(
        id=1, temperatura="quente", status="atendido", has_gap=True,
        lead_name="Cliente Quente", gap="Não fechou visita",
    )
    morno = _mock_analysis(
        id=2, temperatura="morno", status="sem_resposta", has_gap=True,
        lead_name="Cliente Morno", gap="Sem next action",
    )
    frio = _mock_analysis(id=3, temperatura="frio", lead_name="Cliente Frio")

    ctx = build_dona_context([frio, morno, quente])

    totais = ctx["totais_globais"]
    assert totais["leads_quentes"] == 1
    assert totais["leads_mornos_com_gap"] == 1

    top5 = ctx["top_5_leads_detalhados"]
    assert len(top5) == 2  # frio não entra
    assert top5[0]["nome_lead"] == "Cliente Quente"  # quente primeiro
    assert top5[1]["nome_lead"] == "Cliente Morno"
    assert top5[0]["gap"] == "Não fechou visita"


def test_build_context_top_5_limite() -> None:
    """Quando há >5 candidatos (quentes + mornos com gap), retorna apenas 5."""
    from app.agents.agente_assistente import build_dona_context

    analyses = [
        _mock_analysis(id=i, temperatura="quente", status="atendido", has_gap=True)
        for i in range(8)
    ]

    ctx = build_dona_context(analyses)

    assert len(ctx["top_5_leads_detalhados"]) == 5
    assert ctx["totais_globais"]["leads_quentes"] == 8


def test_build_context_com_whatsapp_metrics() -> None:
    """build_dona_context com whatsapp_metrics → totais e performance corretos."""
    from app.agents.agente_assistente import build_dona_context

    analyses = [
        _mock_analysis(id=1, temperatura="quente", corretora="Pessoa10"),
        _mock_analysis(id=2, temperatura="frio", corretora="Pessoa10"),
        _mock_analysis(id=3, temperatura="morno", has_gap=True, corretora="Pessoa11"),
    ]
    wm = {
        "Pessoa10": {"msgs_enviadas": 20, "msgs_recebidas": 10, "contatos_unicos": 15,
                 "leads_responderam": 10, "taxa_resposta_pct": 67},
        "Pessoa11": {"msgs_enviadas": 5, "msgs_recebidas": 3, "contatos_unicos": 4,
                 "leads_responderam": 3, "taxa_resposta_pct": 75},
    }

    ctx = build_dona_context(analyses, whatsapp_metrics=wm)

    totais = ctx["totais_globais"]
    assert totais["msgs_enviadas_total"] == 25
    assert totais["msgs_recebidas_total"] == 13
    assert totais["contatos_unicos_total"] == 19
    assert totais["leads_quentes"] == 1
    assert totais["leads_mornos_com_gap"] == 1

    perf = {p["nome"]: p for p in ctx["performance_por_corretora"]}
    assert perf["Pessoa10"]["msgs_enviadas"] == 20
    assert perf["Pessoa10"]["taxa_resposta_pct"] == 67
    assert perf["Pessoa11"]["contatos_unicos"] == 4


def test_build_context_alerta_taxa_resposta() -> None:
    """Alerta de pipeline: taxa < 25% AND contatos > 5."""
    from app.agents.agente_assistente import build_dona_context

    # Pessoa10: 20% taxa, 10 contatos → DEVE alertar
    # Pessoa11: 20% taxa, 3 contatos → NÃO deve alertar (contatos <= 5)
    # Pessoa3: 30% taxa, 10 contatos → NÃO deve alertar (taxa OK)
    wm = {
        "Pessoa10":     {"msgs_enviadas": 10, "msgs_recebidas": 5, "contatos_unicos": 10,
                     "leads_responderam": 2, "taxa_resposta_pct": 20},
        "Pessoa11":     {"msgs_enviadas": 3,  "msgs_recebidas": 2, "contatos_unicos": 3,
                     "leads_responderam": 1, "taxa_resposta_pct": 20},
        "Pessoa3": {"msgs_enviadas": 10, "msgs_recebidas": 5, "contatos_unicos": 10,
                     "leads_responderam": 3, "taxa_resposta_pct": 30},
    }

    ctx = build_dona_context([], whatsapp_metrics=wm)

    alertas = ctx["alertas_pipeline"]
    assert any("Pessoa10" in a and "pipeline morrendo" in a for a in alertas)
    assert not any("Pessoa11" in a and "pipeline morrendo" in a for a in alertas)
    assert not any("Pessoa3" in a and "pipeline morrendo" in a for a in alertas)


# ── LLM routing ───────────────────────────────────────────────────────────────

def test_briefing_dia_fraco_usa_mini() -> None:
    """Dia sem quentes nem mornos com gap → template MINI, sem chamada ao LLM."""
    from app.agents.agente_assistente import build_dona_context, _is_dia_fraco, _format_briefing_mini

    analyses = [
        _mock_analysis(id=1, temperatura="frio"),
        _mock_analysis(id=2, temperatura="frio"),
        _mock_analysis(id=3, temperatura="morno", has_gap=False),  # morno SEM gap não conta
    ]
    ctx = build_dona_context(analyses)

    assert _is_dia_fraco(ctx) is True

    with patch("app.agents.agente_assistente.get_llm_provider") as mock_llm:
        briefing = _format_briefing_mini(ctx)

    mock_llm.assert_not_called()  # LLM não foi chamado
    assert "Acme Imóveis" in briefing
    assert "Operação tranquila" in briefing


def test_briefing_dia_rico_chama_sonnet() -> None:
    """Dia com lead quente → generate_lead_narrative chama LLM provider."""
    from app.agents.agente_assistente import generate_lead_narrative
    from app.llm.provider import TokenUsage

    raw = {
        "lead_name": "Lead Test",
        "corretora": "Pessoa10",
        "temperatura": "quente",
        "status": "atendido",
        "conversa_resumo": "Lead perguntou sobre imóvel no bairro X e agendou visita.",
        "o_que_corretora_disse": "Tenho uma oportunidade legal a R$190k",
        "gap_identificado": "Não apresentou ficha técnica do imóvel",
    }

    mock_llm = MagicMock()
    mock_llm.complete_text.return_value = (
        "🔥 *Lead Test* — visita marcada amanhã 10h\n\n"
        "Pessoa10 iniciou contato às 14h com imóvel de R$190k.\n\n"
        "Lead confirmou interesse e agendou visita.",
        TokenUsage(input_tokens=500, output_tokens=200),
    )

    with patch("app.agents.agente_assistente.get_llm_provider", return_value=mock_llm):
        text, tokens = generate_lead_narrative(raw)

    mock_llm.complete_text.assert_called_once()
    assert tokens["input"] == 500
    assert tokens["output"] == 200
    assert "Lead Test" in text


# ── fetch_whatsapp_metrics ────────────────────────────────────────────────────

def test_metricas_whatsapp_calculadas_corretamente() -> None:
    """fetch_whatsapp_metrics conta msgs enviadas/recebidas e responderam corretamente."""
    from app.agents.agente_assistente import fetch_whatsapp_metrics

    # midnight SP de 2026-05-06 = 03:00 UTC (SP é UTC-3)
    cutoff = datetime(2026, 5, 6, 3, 0, 0, tzinfo=timezone.utc)

    corretoras = [{"nome": "Pessoa10", "evolution_instance": "acme_corretor",
                   "evolution_connected_since": None, "telefone": "+5500000000000"}]

    mock_chats = [
        {"phone": "+5500000000000", "remote_jid": "+5500000000000@s.whatsapp.net"},
        {"phone": "+5500000000000", "remote_jid": "+5500000000000@s.whatsapp.net"},
    ]
    # Chat 1: corretora 10:00, lead 10:01, corretora 10:02
    #   → cor_today=[10:00,10:02], lead_today=[10:01]
    #   → contato=True (cor presente); respondeu=True (10:01 > 10:00)
    # Chat 2: só corretora (09:00, 09:05), sem lead
    #   → cor_today=[09:00,09:05], lead_today=[]
    #   → contato=True (cor presente); respondeu=False
    mock_conv_1 = [
        {"from": "corretora", "text": "Oi", "at": "2026-05-06T10:00:00+00:00", "type": "text"},
        {"from": "lead",      "text": "ok",  "at": "2026-05-06T10:01:00+00:00", "type": "text"},
        {"from": "corretora", "text": "Ok!", "at": "2026-05-06T10:02:00+00:00", "type": "text"},
    ]
    mock_conv_2 = [
        {"from": "corretora", "text": "Oi",   "at": "2026-05-06T09:00:00+00:00", "type": "text"},
        {"from": "corretora", "text": "Posso ajudar?", "at": "2026-05-06T09:05:00+00:00", "type": "text"},
    ]

    mock_evo = MagicMock()
    mock_evo._client.get_instance_status.return_value = "open"
    mock_evo.list_recent_chats.return_value = mock_chats
    mock_evo.get_conversation.side_effect = [mock_conv_1, mock_conv_2]

    # Mock SessionLocal para isolar do banco real (que pode ter dados históricos)
    mock_session = MagicMock()
    mock_row_1 = MagicMock()
    mock_row_1.instance = "acme_corretor"
    mock_row_1.contatos_unicos = 2
    mock_row_1.msgs_enviadas = 4
    mock_row_1.msgs_recebidas = 1
    mock_row_1.leads_responderam = 1
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.execute.return_value.fetchall.return_value = [mock_row_1]

    with patch("app.agents.agente_assistente.EvolutionTool", return_value=mock_evo), \
         patch("app.agents.agente_assistente.SessionLocal", return_value=mock_session):
        result = fetch_whatsapp_metrics(corretoras, _cutoff_override=cutoff)

    corretor = result["Pessoa10"]
    assert corretor["msgs_enviadas"] == 4       # 2 + 2
    assert corretor["msgs_recebidas"] == 1      # só chat 1 recebeu
    assert corretor["contatos_unicos"] == 2     # ambos têm msgs da corretora
    assert corretor["leads_responderam"] == 1   # só chat 1 (lead falou APÓS corretora)
    assert corretor["taxa_resposta_pct"] == 50  # 1/2 * 100


def test_taxa_resposta_zero_quando_zero_contatos() -> None:
    """Sem contatos no dia → taxa_resposta_pct = 0, sem divisão por zero."""
    from app.agents.agente_assistente import fetch_whatsapp_metrics

    cutoff = datetime(2026, 5, 7, 3, 0, 0, tzinfo=timezone.utc)
    corretoras = [{"nome": "Pessoa10", "evolution_instance": "acme_corretor",
                   "evolution_connected_since": None, "telefone": None}]

    mock_evo = MagicMock()
    mock_evo._client.get_instance_status.return_value = "open"
    mock_evo.list_recent_chats.return_value = []  # nenhum chat

    # Mock SessionLocal para isolar do banco real
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.execute.return_value.fetchall.return_value = []  # zero resultados

    with patch("app.agents.agente_assistente.EvolutionTool", return_value=mock_evo), \
         patch("app.agents.agente_assistente.SessionLocal", return_value=mock_session):
        result = fetch_whatsapp_metrics(corretoras, _cutoff_override=cutoff)

    corretor = result["Pessoa10"]
    assert corretor["contatos_unicos"] == 0
    assert corretor["taxa_resposta_pct"] == 0
    assert corretor["msgs_enviadas"] == 0


def test_corretora_sem_instance_retorna_zerado() -> None:
    """Corretora sem evolution_instance → métricas zeradas sem chamar Evolution."""
    from app.agents.agente_assistente import fetch_whatsapp_metrics

    corretoras = [{"nome": "Pessoa10", "evolution_instance": None,
                   "evolution_connected_since": None, "telefone": None}]

    mock_evo = MagicMock()
    with patch("app.agents.agente_assistente.EvolutionTool", return_value=mock_evo):
        result = fetch_whatsapp_metrics(corretoras)

    assert result["Pessoa10"]["contatos_unicos"] == 0
    mock_evo.list_recent_chats.assert_not_called()


def test_evolution_falha_retorna_zerado_sem_interromper() -> None:
    """Falha na Evolution API → corretora fica zerada, demais corretoras continuam."""
    from app.agents.agente_assistente import fetch_whatsapp_metrics

    cutoff = datetime(2026, 5, 7, 3, 0, 0, tzinfo=timezone.utc)
    corretoras = [
        {"nome": "Pessoa10",  "evolution_instance": "acme_corretor",
         "evolution_connected_since": None, "telefone": None},
        {"nome": "Pessoa11",  "evolution_instance": "acme_corretor",
         "evolution_connected_since": None, "telefone": None},
    ]

    def side_effect(instance: str, **kwargs: object) -> list:
        if instance == "acme_corretor":
            raise RuntimeError("timeout")
        return [{"phone": "+5500000000000", "remote_jid": "+5500000000000@s.whatsapp.net"}]

    mock_evo = MagicMock()
    mock_evo._client.get_instance_status.return_value = "open"
    mock_evo.list_recent_chats.side_effect = side_effect
    # Pessoa11 tem uma msg de corretora para contato ser contabilizado
    mock_evo.get_conversation.return_value = [
        {"from": "corretora", "text": "Oi", "at": "2026-05-07T10:00:00+00:00"},
    ]

    # Mock SessionLocal para isolar do banco real
    # Pessoa10 falha (list_recent_chats raise), Pessoa11 retorna 1 contato via DB
    mock_session = MagicMock()
    mock_row_corretor = MagicMock()
    mock_row_corretor.instance = "acme_corretor"
    mock_row_corretor.contatos_unicos = 1
    mock_row_corretor.msgs_enviadas = 1
    mock_row_corretor.msgs_recebidas = 0
    mock_row_corretor.leads_responderam = 0
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_session.execute.return_value.fetchall.return_value = [mock_row_corretor]

    with patch("app.agents.agente_assistente.EvolutionTool", return_value=mock_evo), \
         patch("app.agents.agente_assistente.SessionLocal", return_value=mock_session):
        result = fetch_whatsapp_metrics(corretoras, _cutoff_override=cutoff)

    assert result["Pessoa10"]["contatos_unicos"] == 0   # zerado por falha
    assert result["Pessoa11"]["contatos_unicos"] == 1   # ainda funcionou


def test_alerta_pipeline_so_dispara_em_cenario_critico() -> None:
    """Alerta dispara só quando taxa < 25% E contatos > 5."""
    from app.agents.agente_assistente import build_dona_context

    # Caso crítico: taxa 20%, 10 contatos → ALERTA
    wm_critico = {"Pessoa10": {"msgs_enviadas": 10, "msgs_recebidas": 5,
                            "contatos_unicos": 10, "leads_responderam": 2,
                            "taxa_resposta_pct": 20}}
    ctx = build_dona_context([], whatsapp_metrics=wm_critico)
    assert any("pipeline morrendo" in a for a in ctx["alertas_pipeline"])

    # Threshold: taxa 20%, mas só 3 contatos → SEM alerta
    wm_poucos = {"Pessoa10": {"msgs_enviadas": 3, "msgs_recebidas": 2,
                           "contatos_unicos": 3, "leads_responderam": 1,
                           "taxa_resposta_pct": 20}}
    ctx2 = build_dona_context([], whatsapp_metrics=wm_poucos)
    assert not any("pipeline morrendo" in a for a in ctx2["alertas_pipeline"])

    # Taxa OK: 30%, 10 contatos → SEM alerta
    wm_ok = {"Pessoa10": {"msgs_enviadas": 10, "msgs_recebidas": 5,
                       "contatos_unicos": 10, "leads_responderam": 3,
                       "taxa_resposta_pct": 30}}
    ctx3 = build_dona_context([], whatsapp_metrics=wm_ok)
    assert not any("pipeline morrendo" in a for a in ctx3["alertas_pipeline"])


# ── run_daily_briefing ────────────────────────────────────────────────────────

def test_dry_run_nao_envia() -> None:
    """dry_run=True não deve chamar TelegramTool.send_to_dona."""
    from app.agents.agente_assistente import run_daily_briefing

    with patch("app.agents.agente_assistente.SessionLocal") as mock_session_cls, \
         patch("app.agents.agente_assistente._fetch_today_analyses") as mock_fetch, \
         patch("app.agents.agente_assistente._fetch_corretoras_with_evolution", return_value=[]), \
         patch("app.agents.agente_assistente.fetch_whatsapp_metrics", return_value={}), \
         patch("app.agents.agente_assistente.TelegramTool") as mock_telegram_cls:

        mock_session = MagicMock()
        mock_session_cls.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_fetch.return_value = []  # dia vazio — sem LLM

        result = run_daily_briefing(dry_run=True)

    mock_telegram_cls.return_value.send_to_dona.assert_not_called()
    assert result["dry_run"] is True
    assert "Acme Imóveis" in result["briefing"]  # deve ter briefing mesmo vazio


def test_marca_consumed_by_dona() -> None:
    """dry_run=False deve chamar send_to_dona e marcar consumed_by_dona nas análises."""
    from app.agents.agente_assistente import run_daily_briefing
    from app.llm.provider import TokenUsage

    analyses = [
        _mock_analysis(id=10, temperatura="frio"),
        _mock_analysis(id=11, temperatura="frio"),
    ]

    mock_llm = MagicMock()
    mock_llm.complete_text.return_value = ("briefing formatado", TokenUsage(100, 50))

    with patch("app.agents.agente_assistente.SessionLocal") as mock_session_cls, \
         patch("app.agents.agente_assistente._fetch_today_analyses") as mock_fetch, \
         patch("app.agents.agente_assistente._fetch_corretoras_with_evolution", return_value=[]), \
         patch("app.agents.agente_assistente.fetch_whatsapp_metrics", return_value={}), \
         patch("app.agents.agente_assistente._already_delivered_today", return_value=False), \
         patch("app.agents.agente_assistente._mark_consumed_dona") as mock_mark, \
         patch("app.agents.agente_assistente.TelegramTool") as mock_telegram_cls:

        mock_session = MagicMock()
        mock_session_cls.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_fetch.return_value = analyses
        mock_telegram_cls.return_value.send_to_dona.return_value = "ok"

        result = run_daily_briefing(dry_run=False)

    # Enviou mensagem
    mock_telegram_cls.return_value.send_to_dona.assert_called_once()

    # Marcou consumed com os IDs corretos
    mock_mark.assert_called_once_with(mock_session, [10, 11])

    assert result["dry_run"] is False
    assert result["leads_processados"] == 2


# ── Cenários de briefing (Onda 1.6) ──────────────────────────────────────────

def _base_patches(analyses: list, wm: dict):
    """Context manager helper que aplica os patches comuns do run_daily_briefing."""
    from contextlib import ExitStack
    from unittest.mock import patch, MagicMock

    stack = ExitStack()
    mock_session = MagicMock()
    mock_session_cls = stack.enter_context(patch("app.agents.agente_assistente.SessionLocal"))
    mock_session_cls.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
    stack.enter_context(patch("app.agents.agente_assistente._fetch_today_analyses",
                              return_value=analyses))
    stack.enter_context(patch("app.agents.agente_assistente._fetch_corretoras_with_evolution",
                              return_value=[]))
    # Onda 7.5 (commit 3e45f936, 2026-05-14) adicionou um caminho de snapshot
    # que TEM PRIORIDADE sobre fetch_whatsapp_metrics quando
    # `_snapshot_disponivel_hoje()` diz que há linha em snapshots_corretora
    # hoje. Sem fixar isso em False, o `SessionLocal` mockado acima faz
    # `_snapshot_disponivel_hoje()` "ver" um COUNT(*) verdadeiro por acidente
    # (MagicMock().scalar() é truthy e int(MagicMock()) == 1 por padrão),
    # então o código ignora silenciosamente o `wm` injetado abaixo e sempre
    # cai no cenário "tudo zerado". Forçar False garante que o teste exercite
    # o fallback (fetch_whatsapp_metrics), que É o que ele quer controlar.
    stack.enter_context(patch("app.agents.agente_assistente._snapshot_disponivel_hoje",
                              return_value=False))
    stack.enter_context(patch("app.agents.agente_assistente.fetch_whatsapp_metrics",
                              return_value=wm))
    stack.enter_context(patch("app.agents.agente_assistente.TelegramTool"))
    return stack, mock_session


def test_cenario_1_tudo_zerado_envia_mini() -> None:
    """Cenário 1: analyses=0, contatos=0 → mensagem única 💤, scenario correto."""
    from app.agents.agente_assistente import run_daily_briefing

    stack, _ = _base_patches(analyses=[], wm={})
    with stack:
        result = run_daily_briefing(dry_run=True)

    assert result["scenario"] == "cenario_1_tudo_zerado"
    assert "💤" in result["briefing_msg1"]
    assert result["briefing_msg2"] is None
    assert result["dia_fraco"] is True


def test_cenario_2_whatsapp_sem_inspector_envia_so_msg1() -> None:
    """Cenário 2: analyses=0, contatos>0 → msg1 com WhatsApp, sem msg2."""
    from app.agents.agente_assistente import run_daily_briefing

    wm = {"Pessoa10": {"msgs_enviadas": 60, "msgs_recebidas": 50, "contatos_unicos": 65,
                   "leads_responderam": 6, "taxa_resposta_pct": 9}}
    stack, _ = _base_patches(analyses=[], wm=wm)
    with stack:
        result = run_daily_briefing(dry_run=True)

    assert result["scenario"] == "cenario_2_whatsapp_sem_inspector"
    assert result["briefing_msg2"] is None
    assert result["dia_fraco"] is True
    assert "Acme Imóveis" in result["briefing_msg1"]
    assert "💤" not in result["briefing_msg1"]  # não é "dia vazio"


def test_cenario_2_inclui_footer_explicativo() -> None:
    """Cenário 2: msg1 deve incluir footer avisando que Inspector ainda não rodou."""
    from app.agents.agente_assistente import run_daily_briefing

    wm = {"Pessoa10": {"msgs_enviadas": 60, "msgs_recebidas": 50, "contatos_unicos": 65,
                   "leads_responderam": 6, "taxa_resposta_pct": 9}}
    stack, _ = _base_patches(analyses=[], wm=wm)
    with stack:
        result = run_daily_briefing(dry_run=True)

    assert "Inspector rodar" in result["briefing_msg1"]
    assert "20h" in result["briefing_msg1"]


def test_cenario_3_tudo_pronto_envia_completo() -> None:
    """Cenário 3: analyses>0, contatos>0 → msg1 + msgs_rich quando dia rico."""
    from app.agents.agente_assistente import run_daily_briefing
    from app.llm.provider import TokenUsage

    analyses = [
        _mock_analysis(id=1, temperatura="quente", has_gap=True,
                       gap="Não fechou visita", corretora="Pessoa10"),
    ]
    wm = {"Pessoa10": {"msgs_enviadas": 60, "msgs_recebidas": 50, "contatos_unicos": 65,
                   "leads_responderam": 6, "taxa_resposta_pct": 9}}

    mock_llm = MagicMock()
    mock_llm.complete_text.return_value = (
        "🔥 *Lead Test* — visita marcada amanhã\n\nJana iniciou contato...\n\nVisita confirmada.",
        TokenUsage(input_tokens=400, output_tokens=150),
    )

    stack, _ = _base_patches(analyses=analyses, wm=wm)
    with stack, patch("app.agents.agente_assistente.get_llm_provider", return_value=mock_llm):
        result = run_daily_briefing(dry_run=True)

    assert result["scenario"] == "cenario_3_tudo_pronto"
    assert result["briefing_msg2"] is not None          # backward compat
    assert result["briefing_msgs_rich"] is not None     # new field
    assert len(result["briefing_msgs_rich"]) >= 1
    assert result["dia_fraco"] is False
    # complete_text é chamado N vezes: 1x por lead + 1x padrão
    assert mock_llm.complete_text.call_count >= 1


def test_edge_inspector_sem_whatsapp_avisa() -> None:
    """Edge case: Inspector rodou mas Evolution caiu → msg1 + aviso, sem '💤'."""
    from app.agents.agente_assistente import run_daily_briefing

    analyses = [
        _mock_analysis(id=1, temperatura="frio"),
        _mock_analysis(id=2, temperatura="frio"),
    ]
    # wm zerado = Evolution caiu (contatos_unicos = 0 em todas)
    wm: dict = {}  # sem instâncias → tudo zerado

    stack, _ = _base_patches(analyses=analyses, wm=wm)
    with stack:
        result = run_daily_briefing(dry_run=True)

    assert result["scenario"] == "edge_inspector_sem_whatsapp"
    assert "Evolution offline" in result["briefing_msg1"]
    assert "💤" not in result["briefing_msg1"]
    assert result["leads_processados"] == 2


# ── fetch_whatsapp_metrics — Fixes 2-5 ───────────────────────────────────────

def test_responder_significa_lead_falou_depois_corretora() -> None:
    """lead_responded conta só se lead mandou msg APÓS a primeira msg da corretora."""
    from app.agents.agente_assistente import fetch_whatsapp_metrics

    cutoff = datetime(2026, 5, 7, 3, 0, 0, tzinfo=timezone.utc)
    corretoras = [{"nome": "Pessoa10", "evolution_instance": "acme_corretor",
                   "evolution_connected_since": None, "telefone": None}]

    # Lead fala às 09:00, corretora responde às 10:00 → lead falou ANTES = não respondeu
    msg_ids = _seed_mensagens_whatsapp("acme_corretor", [
        {"from": "lead", "remote_jid": "+5500000000000@s.whatsapp.net",
         "at": "2026-05-07T09:00:00+00:00"},
        {"from": "corretora", "remote_jid": "+5500000000000@s.whatsapp.net",
         "at": "2026-05-07T10:00:00+00:00"},
    ])
    try:
        mock_evo = MagicMock()
        mock_evo._client.get_instance_status.return_value = "open"
        with patch("app.agents.agente_assistente.EvolutionTool", return_value=mock_evo):
            result = fetch_whatsapp_metrics(corretoras, _cutoff_override=cutoff)
    finally:
        _cleanup_mensagens_whatsapp(msg_ids)

    corretor = result["Pessoa10"]
    assert corretor["contatos_unicos"] == 1      # corretora abordou o lead
    assert corretor["msgs_enviadas"] == 1        # só a msg da corretora
    assert corretor["msgs_recebidas"] == 1       # msg do lead (está no window)
    assert corretor["leads_responderam"] == 0    # lead falou ANTES da corretora = não conta


def test_lead_que_so_respondeu_nao_conta_como_contato() -> None:
    """Chat onde só o lead falou (corretora não abordou hoje) não conta como contato."""
    from app.agents.agente_assistente import fetch_whatsapp_metrics

    cutoff = datetime(2026, 5, 7, 3, 0, 0, tzinfo=timezone.utc)
    corretoras = [{"nome": "Pessoa10", "evolution_instance": "acme_corretor",
                   "evolution_connected_since": None, "telefone": None}]

    mock_chats = [{"phone": "+5500000000000", "remote_jid": "+5500000000000@s.whatsapp.net"}]
    mock_conv = [
        {"from": "lead", "text": "Quero info", "at": "2026-05-07T10:00:00+00:00"},
    ]

    mock_evo = MagicMock()
    mock_evo._client.get_instance_status.return_value = "open"
    mock_evo.list_recent_chats.return_value = mock_chats
    mock_evo.get_conversation.return_value = mock_conv

    with patch("app.agents.agente_assistente.EvolutionTool", return_value=mock_evo):
        result = fetch_whatsapp_metrics(corretoras, _cutoff_override=cutoff)

    corretor = result["Pessoa10"]
    assert corretor["contatos_unicos"] == 0   # corretora não abordou = não conta
    assert corretor["msgs_enviadas"] == 0
    assert corretor["leads_responderam"] == 0


def test_chat_interna_corretora_excluida() -> None:
    """Chat cujo remote_jid/contato bate com telefone de corretora cadastrada é
    excluído — chat corretora↔corretora (ou autochat) não é lead (Fix 5)."""
    from app.agents.agente_assistente import fetch_whatsapp_metrics

    cutoff = datetime(2026, 5, 7, 3, 0, 0, tzinfo=timezone.utc)
    corretoras = [
        {"nome": "Pessoa10", "evolution_instance": "acme_corretor",
         "evolution_connected_since": None, "telefone": "+5500000000000"},
        {"nome": "Pessoa11", "evolution_instance": "acme_corretor",
         "evolution_connected_since": None, "telefone": "+5500000000000"},
    ]

    msg_ids = _seed_mensagens_whatsapp("acme_corretor", [
        # Interno 1: autochat — remote_jid bate com o telefone da PRÓPRIA Pessoa10
        {"from": "corretora", "remote_jid": "+5500000000000@s.whatsapp.net",
         "at": "2026-05-07T09:00:00+00:00"},
        {"from": "lead", "remote_jid": "+5500000000000@s.whatsapp.net",
         "at": "2026-05-07T09:05:00+00:00"},
        # Interno 2: chat corretora↔corretora — remote_jid é o telefone da Pessoa11
        {"from": "corretora", "remote_jid": "+5500000000000@s.whatsapp.net",
         "at": "2026-05-07T09:10:00+00:00"},
        {"from": "lead", "remote_jid": "+5500000000000@s.whatsapp.net",
         "at": "2026-05-07T09:15:00+00:00"},
        # Externo: lead real
        {"from": "corretora", "remote_jid": "+5500000000000@s.whatsapp.net",
         "at": "2026-05-07T10:00:00+00:00"},
        {"from": "lead", "remote_jid": "+5500000000000@s.whatsapp.net",
         "at": "2026-05-07T10:01:00+00:00"},
    ])
    try:
        mock_evo = MagicMock()
        mock_evo._client.get_instance_status.return_value = "open"
        with patch("app.agents.agente_assistente.EvolutionTool", return_value=mock_evo):
            result = fetch_whatsapp_metrics(corretoras, _cutoff_override=cutoff)
    finally:
        _cleanup_mensagens_whatsapp(msg_ids)

    corretor = result["Pessoa10"]
    assert corretor["contatos_unicos"] == 1       # só o lead externo
    assert corretor["msgs_enviadas"] == 1         # só a msg pro lead externo
    assert corretor["msgs_recebidas"] == 1        # só a resposta do lead externo
    assert corretor["leads_responderam"] == 1


def test_janela_dia_sp_nao_pega_mensagens_de_ontem() -> None:
    """Mensagens anteriores à meia-noite SP ficam fora da janela."""
    from app.agents.agente_assistente import fetch_whatsapp_metrics

    # midnight SP 07/05 = 03:00 UTC
    cutoff = datetime(2026, 5, 7, 3, 0, 0, tzinfo=timezone.utc)
    corretoras = [{"nome": "Pessoa10", "evolution_instance": "acme_corretor",
                   "evolution_connected_since": None, "telefone": None}]

    msg_ids = _seed_mensagens_whatsapp("acme_corretor", [
        # De ontem (02:00 UTC = 23:00 SP de ontem) — fora da janela
        {"from": "corretora", "remote_jid": "+5500000000000@s.whatsapp.net",
         "at": "2026-05-07T02:00:00+00:00"},
        {"from": "lead", "remote_jid": "+5500000000000@s.whatsapp.net",
         "at": "2026-05-07T02:30:00+00:00"},
        # De hoje (10:00 UTC = 07:00 SP) — dentro da janela
        {"from": "corretora", "remote_jid": "+5500000000000@s.whatsapp.net",
         "at": "2026-05-07T10:00:00+00:00"},
    ])
    try:
        mock_evo = MagicMock()
        mock_evo._client.get_instance_status.return_value = "open"
        with patch("app.agents.agente_assistente.EvolutionTool", return_value=mock_evo):
            result = fetch_whatsapp_metrics(corretoras, _cutoff_override=cutoff)
    finally:
        _cleanup_mensagens_whatsapp(msg_ids)

    corretor = result["Pessoa10"]
    assert corretor["contatos_unicos"] == 1   # "Bom dia" conta
    assert corretor["msgs_enviadas"] == 1     # só "Bom dia" (de hoje)
    assert corretor["msgs_recebidas"] == 0    # lead respondeu ontem = excluído
    assert corretor["leads_responderam"] == 0


def test_connected_since_de_ontem_nao_restringe_janela() -> None:
    """connected_since de ontem não restringe — cutoff permanece meia-noite SP."""
    from app.agents.agente_assistente import fetch_whatsapp_metrics

    # connected_since = ontem 18:27 UTC (15:27 SP) → antes de midnight SP de hoje
    connected_since = datetime(2026, 5, 6, 18, 27, 0, tzinfo=timezone.utc)
    cutoff = datetime(2026, 5, 7, 3, 0, 0, tzinfo=timezone.utc)  # midnight SP 07/05

    corretoras = [{"nome": "Pessoa10", "evolution_instance": "acme_corretor",
                   "evolution_connected_since": connected_since, "telefone": None}]

    msg_ids = _seed_mensagens_whatsapp("acme_corretor", [
        {"from": "corretora", "remote_jid": "+5500000000000@s.whatsapp.net",
         "at": "2026-05-07T10:00:00+00:00"},
        {"from": "lead", "remote_jid": "+5500000000000@s.whatsapp.net",
         "at": "2026-05-07T10:30:00+00:00"},
    ])
    try:
        mock_evo = MagicMock()
        mock_evo._client.get_instance_status.return_value = "open"
        with patch("app.agents.agente_assistente.EvolutionTool", return_value=mock_evo):
            result = fetch_whatsapp_metrics(corretoras, _cutoff_override=cutoff)
    finally:
        _cleanup_mensagens_whatsapp(msg_ids)

    corretor = result["Pessoa10"]
    # connected_since foi ontem → NÃO deve restringir → ambas as msgs contam
    assert corretor["contatos_unicos"] == 1
    assert corretor["msgs_enviadas"] == 1
    assert corretor["msgs_recebidas"] == 1
    assert corretor["leads_responderam"] == 1  # lead respondeu após corretora


def test_connected_since_de_hoje_restringe_janela() -> None:
    """connected_since posterior à meia-noite SP restringe (corretora reconectou hoje)."""
    from app.agents.agente_assistente import fetch_whatsapp_metrics

    # connected_since = hoje 08:00 UTC (05:00 SP) → DEPOIS de midnight SP
    connected_since = datetime(2026, 5, 7, 8, 0, 0, tzinfo=timezone.utc)
    cutoff = datetime(2026, 5, 7, 3, 0, 0, tzinfo=timezone.utc)  # midnight SP 07/05

    corretoras = [{"nome": "Pessoa10", "evolution_instance": "acme_corretor",
                   "evolution_connected_since": connected_since, "telefone": None}]

    msg_ids = _seed_mensagens_whatsapp("acme_corretor", [
        # Antes de connected_since (06:00 UTC) → excluído
        {"from": "corretora", "remote_jid": "+5500000000000@s.whatsapp.net",
         "text": "Antes", "at": "2026-05-07T06:00:00+00:00"},
        {"from": "lead", "remote_jid": "+5500000000000@s.whatsapp.net",
         "text": "Resp", "at": "2026-05-07T07:00:00+00:00"},
        # Após connected_since (09:00 UTC) → dentro da janela
        {"from": "corretora", "remote_jid": "+5500000000000@s.whatsapp.net",
         "text": "Olá", "at": "2026-05-07T09:00:00+00:00"},
        {"from": "lead", "remote_jid": "+5500000000000@s.whatsapp.net",
         "text": "Oi", "at": "2026-05-07T09:30:00+00:00"},
    ])
    try:
        mock_evo = MagicMock()
        mock_evo._client.get_instance_status.return_value = "open"
        with patch("app.agents.agente_assistente.EvolutionTool", return_value=mock_evo):
            result = fetch_whatsapp_metrics(corretoras, _cutoff_override=cutoff)
    finally:
        _cleanup_mensagens_whatsapp(msg_ids)

    corretor = result["Pessoa10"]
    assert corretor["msgs_enviadas"] == 1     # só "Olá" (após connected_since)
    assert corretor["msgs_recebidas"] == 1    # só "Oi" (após connected_since)
    assert corretor["leads_responderam"] == 1  # "Oi" > "Olá"


# ── P1: instance offline → métricas zeradas com is_offline=True ──────────────

def test_metricas_zero_quando_instancia_offline() -> None:
    """P1: instância com state='close' retorna is_offline=True e contatos=0."""
    from app.agents.agente_assistente import fetch_whatsapp_metrics

    cutoff = datetime(2026, 5, 7, 3, 0, 0, tzinfo=timezone.utc)
    corretoras = [{"nome": "Pessoa10", "evolution_instance": "acme_corretor",
                   "evolution_connected_since": None, "telefone": None}]

    mock_evo = MagicMock()
    mock_evo._client.get_instance_status.return_value = "close"
    # list_recent_chats NÃO deve ser chamado — dados stale

    with patch("app.agents.agente_assistente.EvolutionTool", return_value=mock_evo):
        result = fetch_whatsapp_metrics(corretoras, _cutoff_override=cutoff)

    corretor = result["Pessoa10"]
    assert corretor["is_offline"] is True
    assert corretor["instance_state"] == "close"
    assert corretor["contatos_unicos"] == 0
    assert corretor["msgs_enviadas"] == 0
    mock_evo.list_recent_chats.assert_not_called()


def test_instancia_open_nao_marca_offline() -> None:
    """P1: instância 'open' → is_offline=False, métricas calculadas normalmente."""
    from app.agents.agente_assistente import fetch_whatsapp_metrics

    cutoff = datetime(2026, 5, 7, 3, 0, 0, tzinfo=timezone.utc)
    corretoras = [{"nome": "Pessoa11", "evolution_instance": "acme_corretor",
                   "evolution_connected_since": None, "telefone": None}]

    msg_ids = _seed_mensagens_whatsapp("acme_corretor", [
        {"from": "corretora", "remote_jid": "+5500000000000@s.whatsapp.net",
         "at": "2026-05-07T10:00:00+00:00"},
    ])
    try:
        mock_evo = MagicMock()
        mock_evo._client.get_instance_status.return_value = "open"
        with patch("app.agents.agente_assistente.EvolutionTool", return_value=mock_evo):
            result = fetch_whatsapp_metrics(corretoras, _cutoff_override=cutoff)
    finally:
        _cleanup_mensagens_whatsapp(msg_ids)

    corretor = result["Pessoa11"]
    assert corretor["is_offline"] is False
    assert corretor["contatos_unicos"] == 1


# ── P3: visual offline e alerta sem falso positivo ────────────────────────────

def test_briefing_sinaliza_corretora_offline() -> None:
    """P3: _format_msg1_metrics mostra '⚠️ INSTÂNCIA OFFLINE' em vez de zeros."""
    from app.agents.agente_assistente import build_dona_context, _format_msg1_metrics

    wm = {
        "Pessoa10": {
            "msgs_enviadas": 0, "msgs_recebidas": 0, "contatos_unicos": 0,
            "leads_responderam": 0, "taxa_resposta_pct": 0,
            "is_offline": True, "instance_state": "close",
        },
    }
    ctx = build_dona_context([], whatsapp_metrics=wm)
    msg = _format_msg1_metrics(ctx)

    assert "INSTÂNCIA OFFLINE" in msg
    assert "Pessoa10" in msg
    assert "Evolution Manager" in msg
    # Não deve mostrar "nenhuma atividade" (que seria enganoso)
    assert "nenhuma atividade" not in msg


def test_alerta_pipeline_nao_dispara_quando_offline() -> None:
    """P3: corretora offline NÃO gera alerta 'pipeline morrendo' (dados stale)."""
    from app.agents.agente_assistente import build_dona_context

    # Pessoa10 offline — sem dados reais → não deve gerar alerta de taxa baixa
    wm = {
        "Pessoa10": {
            "msgs_enviadas": 0, "msgs_recebidas": 0, "contatos_unicos": 0,
            "leads_responderam": 0, "taxa_resposta_pct": 0,
            "is_offline": True, "instance_state": "close",
        },
    }
    ctx = build_dona_context([], whatsapp_metrics=wm)

    alertas = ctx["alertas_pipeline"]
    assert not any("pipeline morrendo" in a for a in alertas)
    # Deve ter alerta de offline em vez disso
    assert any("desconectada" in a for a in alertas)


# ── P2: internal_contacts excluídos das métricas ─────────────────────────────

def test_internal_contacts_excluidos_no_metrics() -> None:
    """P2: telefone cadastrado em internos_acme é excluído mesmo sem corretora.

    `extra_internal_phones` está deprecated (a exclusão real hoje é via SQL
    contra `internos_acme` — ver docstring de fetch_whatsapp_metrics), então
    o teste semeia a tabela em vez de passar o parâmetro (que o código ignora).
    """
    from app.agents.agente_assistente import fetch_whatsapp_metrics

    cutoff = datetime(2026, 5, 7, 3, 0, 0, tzinfo=timezone.utc)
    corretoras = [{"nome": "Pessoa11", "evolution_instance": "acme_corretor",
                   "evolution_connected_since": None, "telefone": None}]

    interno_tel = "+5500000000000"
    _seed_interno(interno_tel, nome="Assistente")
    msg_ids = _seed_mensagens_whatsapp("acme_corretor", [
        # Interno: Assistente (coordenadora) — deve ser excluído
        {"from": "corretora", "remote_jid": "+5500000000000@s.whatsapp.net",
         "contato_telefone": interno_tel, "at": "2026-05-07T10:00:00+00:00"},
        # Externo: lead real
        {"from": "corretora", "remote_jid": "+5500000000000@s.whatsapp.net",
         "at": "2026-05-07T10:00:00+00:00"},
    ])
    try:
        mock_evo = MagicMock()
        mock_evo._client.get_instance_status.return_value = "open"
        with patch("app.agents.agente_assistente.EvolutionTool", return_value=mock_evo):
            result = fetch_whatsapp_metrics(corretoras, _cutoff_override=cutoff)
    finally:
        _cleanup_mensagens_whatsapp(msg_ids)
        _cleanup_interno(interno_tel)

    corretor = result["Pessoa11"]
    assert corretor["contatos_unicos"] == 1            # só lead externo


def test_lid_de_interno_excluido() -> None:
    """P2: chat @lid cujo telefone resolvido é interno é excluído.

    A resolução @lid→telefone acontece na INGESTÃO (mensagens_evolution_service,
    extrair_telefone_lid), não em fetch_whatsapp_metrics — por isso o teste
    semeia a linha já com `contato_telefone` resolvido (é isso que chega no
    banco em produção) e a exclusão real é via `internos_acme` (mesma nota
    de `test_internal_contacts_excluidos_no_metrics`)."""
    from app.agents.agente_assistente import fetch_whatsapp_metrics

    cutoff = datetime(2026, 5, 7, 3, 0, 0, tzinfo=timezone.utc)
    corretoras = [{"nome": "Pessoa3", "evolution_instance": "acme_corretora_a",
                   "evolution_connected_since": None, "telefone": None}]

    # @lid chat cujo phone resolve para número interno (Pessoa Pessoa9)
    internal_phone = "+5500000000000"
    _seed_interno(internal_phone, nome="Pessoa Pessoa9 Interna Teste")
    msg_ids = _seed_mensagens_whatsapp("acme_corretora_a", [
        {"from": "corretora", "remote_jid": "lid_opaque_abc123@lid",
         "contato_telefone": internal_phone, "at": "2026-05-07T10:00:00+00:00"},
        {"from": "corretora", "remote_jid": "+5500000000000@s.whatsapp.net",
         "at": "2026-05-07T10:00:00+00:00"},
        {"from": "lead", "remote_jid": "+5500000000000@s.whatsapp.net",
         "at": "2026-05-07T10:01:00+00:00"},
    ])
    try:
        mock_evo = MagicMock()
        mock_evo._client.get_instance_status.return_value = "open"
        with patch("app.agents.agente_assistente.EvolutionTool", return_value=mock_evo):
            result = fetch_whatsapp_metrics(corretoras, _cutoff_override=cutoff)
    finally:
        _cleanup_mensagens_whatsapp(msg_ids)
        _cleanup_interno(internal_phone)

    corretora_a = result["Pessoa3"]
    # @lid de Pessoa Pessoa9 excluído — só o lead externo conta
    assert corretora_a["contatos_unicos"] == 1


# ── Onda 1.7 — Narrativas ─────────────────────────────────────────────────────

def test_narrativa_nao_inclui_sugestao_explicita() -> None:
    """System prompt de narrativa proíbe explicitamente 💡 sugestão e recomendação."""
    from app.agents.agente_assistente import _NARRATIVE_SYSTEM

    assert "💡 sugestão" in _NARRATIVE_SYSTEM or "sugestão" in _NARRATIVE_SYSTEM
    assert "NÃO inclui" in _NARRATIVE_SYSTEM or "EVITAR" in _NARRATIVE_SYSTEM
    assert "recomendação" in _NARRATIVE_SYSTEM or "sugestão" in _NARRATIVE_SYSTEM


def test_narrativa_pode_citar_aspas_quando_relevante() -> None:
    """generate_lead_narrative passa citações com aspas sem alteração."""
    from app.agents.agente_assistente import generate_lead_narrative
    from app.llm.provider import TokenUsage

    raw = {
        "lead_name": "Emerson",
        "corretora": "Pessoa3",
        "temperatura": "quente",
        "status": "atendido",
        "conversa_resumo": "Lead retomou contato após follow-ups.",
        "o_que_corretora_disse": "Vamos fazer sim",
    }

    mock_llm = MagicMock()
    expected = (
        '🔥 *Emerson* — documentação enviada, negócio em andamento\n\n'
        'Pessoa3 fez dois follow-ups antes de Emerson retomar às 15h.\n\n'
        'Ele confirmou interesse dizendo "Vamos fazer sim" e pediu lista de docs.'
    )
    mock_llm.complete_text.return_value = (
        expected,
        TokenUsage(input_tokens=300, output_tokens=100),
    )

    with patch("app.agents.agente_assistente.get_llm_provider", return_value=mock_llm):
        text, _ = generate_lead_narrative(raw)

    assert '"Vamos fazer sim"' in text
    assert "💡" not in text


def test_narrativa_tem_2_a_3_paragrafos() -> None:
    """System prompt especifica 2-3 parágrafos; generate_lead_narrative preserva output do LLM."""
    from app.agents.agente_assistente import _NARRATIVE_SYSTEM, generate_lead_narrative
    from app.llm.provider import TokenUsage

    assert "2-3 parágrafos" in _NARRATIVE_SYSTEM

    raw = {"lead_name": "Nicoly", "corretora": "Pessoa11", "temperatura": "quente",
           "status": "sem_resposta", "conversa_resumo": "Visita marcada às 9h."}

    two_paras = "🔥 *Nicoly* — visita confirmada amanhã 9h\n\nClau alinhou horário às 18h.\n\nNicoly confirmou com entusiasmo."

    mock_llm = MagicMock()
    mock_llm.complete_text.return_value = (two_paras, MagicMock(input_tokens=200, output_tokens=80))

    with patch("app.agents.agente_assistente.get_llm_provider", return_value=mock_llm):
        text, _ = generate_lead_narrative(raw)

    paragrafos = [p for p in text.split("\n\n") if p.strip()]
    assert 2 <= len(paragrafos) <= 3


def test_padrao_do_dia_em_mensagem_separada() -> None:
    """_format_briefing_rich: quando há padrão, ele está na última mensagem, separado das narrativas."""
    from app.agents.agente_assistente import _format_briefing_rich, build_dona_context
    from app.llm.provider import TokenUsage

    analyses = [
        _mock_analysis(id=1, temperatura="quente", has_gap=True,
                       gap="Não fechou visita", corretora="Pessoa10"),
    ]
    ctx = build_dona_context(analyses)

    mock_llm = MagicMock()

    def side_effect(system: str, user: str) -> tuple:
        if "jornalismo" in system or "NARRATIVA" in system:
            # narrative call
            return (
                "🔥 *Lead Test* — visita pendente\n\nJana abordou às 14h.\n\nLead respondeu.",
                TokenUsage(input_tokens=300, output_tokens=80),
            )
        # padrao call
        return (
            "3 conversas do dia terminaram sem proposta de próxima ação.",
            TokenUsage(input_tokens=150, output_tokens=40),
        )

    mock_llm.complete_text.side_effect = side_effect

    with patch("app.agents.agente_assistente.get_llm_provider", return_value=mock_llm):
        msgs, token_stats = _format_briefing_rich(ctx)

    # Deve haver pelo menos 2 mensagens: narrativa(s) + padrão
    assert len(msgs) >= 2
    ultima = msgs[-1]
    assert "💡 *PADRÃO QUE NOTEI HOJE*" in ultima
    # Nenhuma das mensagens de narrativa tem o bloco de padrão
    for msg in msgs[:-1]:
        assert "💡 *PADRÃO QUE NOTEI HOJE*" not in msg
    # Token stats acumulados
    assert token_stats["input"] > 0
    assert token_stats["output"] > 0


def test_complete_text_via_cli_subprocess_mock() -> None:
    """complete_text() flui pelo ClaudeCliProvider real; mock apenas no subprocess.run."""
    from app.agents.agente_assistente import generate_lead_narrative
    from app.llm.cli_provider import ClaudeCliProvider

    raw = {
        "lead_name": "Teste Sub",
        "corretora": "Pessoa10",
        "temperatura": "quente",
        "status": "atendido",
        "conversa_resumo": "Lead agendou visita.",
    }

    fake_output = "🔥 *Teste Sub* — visita marcada\n\nJana confirmou às 14h.\n\nLead confirmou."
    mock_proc = MagicMock()
    mock_proc.stdout = fake_output
    mock_proc.returncode = 0

    with patch("app.agents.agente_assistente.get_llm_provider", return_value=ClaudeCliProvider(timeout=30)), \
         patch("subprocess.run", return_value=mock_proc):
        text, tokens = generate_lead_narrative(raw)

    assert isinstance(text, str)
    assert len(text) > 0
    assert tokens["input"] > 0
    assert tokens["output"] > 0


def test_split_quando_narrativas_grandes() -> None:
    """_build_narratives_messages divide em 2+ mensagens quando conteúdo > 3500 chars."""
    from app.agents.agente_assistente import _build_narratives_messages, _NARRATIVE_SPLIT_THRESHOLD

    # Dois leads com conteúdo distinto — cada um com 1800 chars
    lead1 = "A" * 1800
    lead2 = "B" * 1800

    narrativas = [
        {"temperatura": "quente", "narrative": lead1},
        {"temperatura": "quente", "narrative": lead2},
    ]

    msgs = _build_narratives_messages(narrativas, "07/05/2026")

    assert len(msgs) > 1, "Deveria ter dividido em múltiplas mensagens"
    for msg in msgs:
        assert len(msg) <= _NARRATIVE_SPLIT_THRESHOLD + 200  # margem pra headers
    # Cada lead deve aparecer inteiro em exatamente uma mensagem
    for narrative in [lead1, lead2]:
        found = sum(1 for m in msgs if narrative in m)
        assert found == 1, "Lead deve aparecer inteiro em exatamente uma mensagem"
