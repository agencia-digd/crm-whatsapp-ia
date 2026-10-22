"""
Golden tests — app/delivery/dona_formatter.py (Onda 2 · squad Acme).

Provam que a formatação do briefing da Dona, dado um contexto FIXO, produz o
texto EXATO capturado ANTES da extração de agente_assistente.py (zero drift).

O provider LLM é sempre mockado — estes testes são puros, sem DB nem rede.
`metrica_adocao_semana` é mockada pra None (comportamento sem bloco de adoção).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.delivery import dona_formatter as fmt
from app.llm.provider import TokenUsage


# ── Deterministas (sem LLM) ───────────────────────────────────────────────────

def test_format_msg1_metrics_golden(ctx_dona_rich, golden_dona) -> None:
    with patch("app.services.adocao_metricas.metrica_adocao_semana", return_value=None):
        out = fmt.format_msg1_metrics(ctx_dona_rich, dia_fraco=False)
    assert out == golden_dona["msg1_rich"]


def _ctx_min(perf: list[dict]) -> dict:
    return {
        "data": "03/07",
        "totais_globais": {
            "msgs_enviadas_total": 1, "msgs_recebidas_total": 1,
            "contatos_unicos_total": 1, "leads_quentes": 0,
            "leads_mornos_com_gap": 0,
        },
        "performance_por_corretora": perf,
        "alertas_pipeline": {},
    }


def _perf_corretora(**over) -> dict:
    base = {
        "nome": "Pessoa12", "msgs_enviadas": 5, "msgs_recebidas": 3,
        "contatos_unicos": 2, "leads_responderam": 1, "taxa_resposta_pct": 50,
        "quentes": 0, "mornos_com_gap": 0, "tarefas_atrasadas": 0,
        "is_offline": False, "instance_state": "open",
    }
    base.update(over)
    return base


def test_msg1_avisa_audio_canal_cloud() -> None:
    """Corretora canal Cloud (audio_pode_faltar) → briefing mostra o aviso."""
    ctx = _ctx_min([_perf_corretora(audio_pode_faltar=True)])
    with patch("app.services.adocao_metricas.metrica_adocao_semana", return_value=None):
        out = fmt.format_msg1_metrics(ctx, dia_fraco=False)
    assert "áudios podem não estar contabilizados" in out


def test_msg1_sem_aviso_audio_para_evolution() -> None:
    """Corretora Evolution (sem flag) → sem aviso de áudio."""
    ctx = _ctx_min([_perf_corretora(audio_pode_faltar=False)])
    with patch("app.services.adocao_metricas.metrica_adocao_semana", return_value=None):
        out = fmt.format_msg1_metrics(ctx, dia_fraco=False)
    assert "áudios podem não estar contabilizados" not in out


def test_format_briefing_mini_golden(ctx_dona_fraco, golden_dona) -> None:
    with patch("app.services.adocao_metricas.metrica_adocao_semana", return_value=None):
        out = fmt.format_briefing_mini(ctx_dona_fraco)
    assert out == golden_dona["mini"]


def test_build_narratives_messages_golden(narratives_fixture, golden_dona) -> None:
    out = fmt.build_narratives_messages(narratives_fixture, "12/05/2026")
    assert out == golden_dona["narratives"]


def test_build_padrao_message_golden(golden_dona) -> None:
    assert fmt.build_padrao_message("Padrão X detectado.") == golden_dona["padrao_msg"]


def test_is_dia_fraco(ctx_dona_rich, ctx_dona_fraco) -> None:
    assert fmt.is_dia_fraco(ctx_dona_fraco) is True
    assert fmt.is_dia_fraco(ctx_dona_rich) is False


# ── LLM prompt builders (provider injetado, mockado) ──────────────────────────

def test_generate_lead_narrative_prompt_golden(ctx_dona_rich, golden_dona) -> None:
    raw = ctx_dona_rich["top_5_leads_detalhados"][0]["raw_analysis"]
    mock_llm = MagicMock()
    mock_llm.complete_text.return_value = (
        "NARRATIVA_GERADA", TokenUsage(input_tokens=10, output_tokens=5)
    )
    text, tokens = fmt.generate_lead_narrative(raw, provider=mock_llm)

    system, user_prompt = mock_llm.complete_text.call_args[0]
    assert system == golden_dona["gen_narrative_system"]
    assert user_prompt == golden_dona["gen_narrative_user_prompt"]
    assert text == "NARRATIVA_GERADA"
    assert tokens == {"input": 10, "output": 5}


def test_generate_padrao_do_dia_prompt_golden(ctx_dona_rich, golden_dona) -> None:
    mock_llm = MagicMock()
    mock_llm.complete_text.return_value = (
        "PADRAO", TokenUsage(input_tokens=3, output_tokens=2)
    )
    padrao, tokens = fmt.generate_padrao_do_dia(ctx_dona_rich, provider=mock_llm)

    _system, user_prompt = mock_llm.complete_text.call_args[0]
    assert user_prompt == golden_dona["gen_padrao_user_prompt"]
    assert padrao == "PADRAO"
    assert tokens == {"input": 3, "output": 2}


def test_generate_padrao_do_dia_nenhum_padrao(ctx_dona_rich) -> None:
    mock_llm = MagicMock()
    mock_llm.complete_text.return_value = (
        "NENHUM_PADRAO", TokenUsage(input_tokens=1, output_tokens=1)
    )
    padrao, _ = fmt.generate_padrao_do_dia(ctx_dona_rich, provider=mock_llm)
    assert padrao is None


# ── Trava dura de manchete (conversa, não etapa/automação) ───────────────────

_BOA = (
    "🔥 *João* · Pessoa10 — quer apto 2q na Bairro X, pediu valores\n\n"
    "Ele escreveu 'quero apto na Bairro X'.\n\n"
    "_card em Follow-up_"
)


def _raw_lead() -> dict:
    return {
        "lead_name": "João", "corretora": "Pessoa10", "temperatura": "quente",
        "status": "atendido",
        "conversa_resumo": "Lead disse 'quero apto na Bairro X' e pediu valores",
        "o_que_corretora_disse": "Te mando 3 opções ainda hoje",
        "etapa_nome": "Follow-up",
    }


@pytest.mark.parametrize("ruim", [
    "lead da Pessoa1 no Follow-up\n\nP1.",
    "📋 *Pessoa1* · Pessoa — no follow-up desde ontem\n\nP1.",
    "📋 *Pessoa1* · Pessoa — automação funcionou\n\nP1.",
])
def test_headline_proibida_regera_uma_vez(ruim: str) -> None:
    """Manchete de etapa/automação → 1 regeração com o system seco."""
    assert fmt._headline_proibida(ruim) is True

    mock_llm = MagicMock()
    mock_llm.complete_text.side_effect = [
        (ruim, TokenUsage(input_tokens=10, output_tokens=5)),
        (_BOA, TokenUsage(input_tokens=8, output_tokens=4)),
    ]
    text, tokens = fmt.generate_lead_narrative(_raw_lead(), provider=mock_llm)

    assert text == _BOA
    assert mock_llm.complete_text.call_count == 2
    assert mock_llm.complete_text.call_args_list[1][0][0] == fmt._NARRATIVE_SYSTEM_SECO
    assert tokens == {"input": 18, "output": 9}


def test_headline_ok_nao_regera() -> None:
    """Manchete boa (etapa só no rodapé) → uma única chamada."""
    mock_llm = MagicMock()
    mock_llm.complete_text.return_value = (_BOA, TokenUsage(input_tokens=10, output_tokens=5))
    text, tokens = fmt.generate_lead_narrative(_raw_lead(), provider=mock_llm)
    assert text == _BOA
    assert mock_llm.complete_text.call_count == 1
    assert tokens == {"input": 10, "output": 5}


def test_headline_proibida_false_quando_etapa_so_no_rodape() -> None:
    assert fmt._headline_proibida(_BOA) is False


def test_fallback_deterministico_com_conversa_real() -> None:
    """LLM insiste 2x na manchete errada → fallback cita a fala do lead."""
    ruim = "lead da Pessoa1 no Follow-up\n\nA automação mandou."
    mock_llm = MagicMock()
    mock_llm.complete_text.side_effect = [
        (ruim, TokenUsage(input_tokens=10, output_tokens=5)),
        (ruim, TokenUsage(input_tokens=8, output_tokens=4)),
    ]
    text, tokens = fmt.generate_lead_narrative(_raw_lead(), provider=mock_llm)

    manchete = text.splitlines()[0]
    assert "Follow-up" not in manchete
    assert "quero apto na Bairro X" in text
    assert text.rstrip().endswith("_card em Follow-up_")
    assert tokens == {"input": 18, "output": 9}


def test_fallback_so_automacao_cita_o_texto_enviado() -> None:
    """Só houve mensagem automática → cita o TEXTO, nunca 'automação funcionou'."""
    raw = {
        "lead_name": "Pessoa1", "corretora": "Pessoa", "temperatura": "morno",
        "status": "sem_resposta",
        "etapa": "Follow-up",
        "conversa": [
            {"from": "bot", "text": "Oi, sou a Corretor da Acme, vi seu interesse no apto",
             "at": "2026-08-18T13:00:00Z"},
            {"from": "automacao", "text": "Posso te mandar as opções?",
             "at": "2026-08-18T13:01:00Z"},
        ],
    }
    ruim = "lead da Pessoa1 no Follow-up\n\nP1."
    mock_llm = MagicMock()
    mock_llm.complete_text.side_effect = [
        (ruim, TokenUsage(input_tokens=1, output_tokens=1)),
        (ruim, TokenUsage(input_tokens=1, output_tokens=1)),
    ]
    text, _ = fmt.generate_lead_narrative(raw, provider=mock_llm)

    assert "Oi, sou a Corretor da Acme, vi seu interesse no apto" in text
    assert "automação funcionou" not in text.lower()
    assert "a automação mandou" not in text.lower()
    assert "Follow-up" not in text.splitlines()[0]
    assert text.rstrip().endswith("_card em Follow-up_")


def test_etapa_crm_ignora_id_numerico() -> None:
    assert fmt._etapa_crm({"etapa_kommo": "12345"}) == ""
    assert fmt._etapa_crm({"etapa_kommo": " Novo "}) == "Novo"
    assert fmt._etapa_crm({"etapa_nome": "Follow-up", "etapa": "X"}) == "Follow-up"
    assert fmt._etapa_crm({}) == ""


def test_conversa_bloco_sem_conversa() -> None:
    assert fmt.format_lead_conversa_bloco({}) == "(sem conversa registrada)"


# ── Orquestração rica ─────────────────────────────────────────────────────────

def test_format_briefing_rich_golden(ctx_dona_rich, golden_dona) -> None:
    mock_llm = MagicMock()
    mock_llm.complete_text.return_value = (
        "RICHGEN", TokenUsage(input_tokens=7, output_tokens=4)
    )
    msgs, stats = fmt.format_briefing_rich(ctx_dona_rich, provider=mock_llm)
    assert msgs == golden_dona["rich_msgs"]
    assert stats == golden_dona["rich_stats"]


# ── Compatibilidade: nomes históricos seguem no agente ────────────────────────

def test_agente_assistente_reexports_preservados() -> None:
    """agente_assistente segue sendo entry-point: re-exporta os nomes antigos."""
    import app.agents.agente_assistente as ab
    for nome in (
        "_format_msg1_metrics", "_format_briefing_mini", "_format_briefing_rich",
        "_build_narratives_messages", "_build_padrao_message", "_is_dia_fraco",
        "_fmt_sp", "_NARRATIVE_SYSTEM", "_NARRATIVE_SPLIT_THRESHOLD",
        "SYSTEM_PROMPT_DONA", "generate_lead_narrative", "generate_padrao_do_dia",
    ):
        assert hasattr(ab, nome), f"agente_assistente perdeu re-export: {nome}"
