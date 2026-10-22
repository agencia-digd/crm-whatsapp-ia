"""Fixtures compartilhadas dos testes golden de delivery.

Contexto/dados FIXOS (idênticos aos usados pra capturar os goldens ANTES da
extração Onda 2). Se algum formatter mudar de output, o golden pega o drift.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

GOLDEN_DIR = Path(__file__).parent / "golden"


def load_golden(name: str) -> dict[str, Any]:
    with (GOLDEN_DIR / name).open(encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def golden_dona() -> dict[str, Any]:
    return load_golden("golden_dona.json")


@pytest.fixture
def golden_corretora() -> dict[str, Any]:
    return load_golden("golden_corretora.json")


# ── Contexto FIXO do briefing da Dona (dict de build_dona_context) ────────────

@pytest.fixture
def ctx_dona_rich() -> dict[str, Any]:
    return {
        "data": "12/05/2026",
        "totais_globais": {
            "msgs_enviadas_total": 120, "msgs_recebidas_total": 80,
            "contatos_unicos_total": 40, "leads_quentes": 2,
            "leads_mornos_com_gap": 1, "tarefas_atrasadas_total": 3,
        },
        "performance_por_corretora": [
            {"nome": "Pessoa10", "msgs_enviadas": 60, "msgs_recebidas": 40,
             "contatos_unicos": 20, "leads_responderam": 8, "taxa_resposta_pct": 40,
             "quentes": 1, "mornos_com_gap": 1, "tarefas_atrasadas": 3,
             "is_offline": False, "instance_state": "open",
             "media_envios_14d": None, "media_recebidos_14d": None, "media_taxa_14d": None},
            {"nome": "Pessoa11", "msgs_enviadas": 0, "msgs_recebidas": 0,
             "contatos_unicos": 0, "leads_responderam": 0, "taxa_resposta_pct": 0,
             "quentes": 1, "mornos_com_gap": 0, "tarefas_atrasadas": 0,
             "is_offline": True, "instance_state": "close",
             "media_envios_14d": None, "media_recebidos_14d": None, "media_taxa_14d": None},
        ],
        "top_5_leads_detalhados": [
            {"nome_lead": "João", "corretora": "Pessoa10", "temperatura": "quente",
             "modo": "", "gap": "Não fechou visita", "sugestao": "Sugira horário",
             "raw_analysis": {
                 "lead_name": "João", "corretora": "Pessoa10", "temperatura": "quente",
                 "status": "atendido", "conversa_resumo": "Quer apto 2q Bairro X",
                 "o_que_corretora_disse": "Mando opções",
                 "gap_identificado": "Não fechou visita"}},
        ],
        "alertas_pipeline": ["Pessoa10: 3 tarefa(s) Kommo vencida(s) sem contato"],
    }


@pytest.fixture
def ctx_dona_fraco() -> dict[str, Any]:
    return {
        "data": "12/05/2026",
        "totais_globais": {
            "msgs_enviadas_total": 0, "msgs_recebidas_total": 0,
            "contatos_unicos_total": 0, "leads_quentes": 0,
            "leads_mornos_com_gap": 0, "tarefas_atrasadas_total": 0,
        },
        "performance_por_corretora": [],
        "top_5_leads_detalhados": [],
        "alertas_pipeline": [],
    }


@pytest.fixture
def narratives_fixture() -> list[dict[str, Any]]:
    return [
        {"nome_lead": "João", "temperatura": "quente",
         "narrative": "🔥 *João* · Pessoa10 — visita\n\nP1.\n\nP2."},
        {"nome_lead": "Corretor", "temperatura": "morno",
         "narrative": "🌡️ *Corretor* · Pessoa11 — morna\n\nP1.\n\nP2."},
    ]
