"""
Teste de regressão — Codex #8: o cache de mensagens do Inspector não pode
pular um lead que tem tarefa Kommo atrasada.

Bug (antes do fix em app/inspector/pipeline.py): `should_reanalyze` decide
sozinho, só olhando timestamp de MENSAGEM (mensagens_whatsapp vs.
lead_analyses.analyzed_at), se o lead deve ser pulado. As tarefas atrasadas do
Kommo são buscadas frescas a cada run (ficam mais atrasadas, ou surgem novas)
e não fazem parte da chave do cache. Um lead sem mensagem (já analisado 1x
como sem_atividade/tarefa_atrasada) ficava preso no cache PRA SEMPRE — mesmo
que uma tarefa atrasada aparecesse depois, o lead nunca mais era reavaliado
e a corretora nunca via o alerta no briefing.

Fix: o pipeline busca as tarefas atrasadas do lead ANTES de decidir se usa o
cache. Se houver alguma tarefa atrasada, o cache é ignorado (bypass) e o lead
segue pro fluxo normal — que cai no modo `tarefa_atrasada` (determinístico,
zero LLM), então o bypass não tem custo de chamada LLM.

Sem DB real, sem LLM real — tudo mockado (mesmo padrão de test_inspector_lock.py).
"""
import uuid
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

from app.inspector.pipeline import (
    _get_lead_tasks_detail,
    _parse_tasks_for_lead,
    _process_corretora,
)


_CORRETORA = {
    "id": 3,
    "nome": "Pessoa3",
    "evolution_instance": "acme_corretora_a",
    "kommo_user_id": 10000003,
    "evolution_connected_since": datetime(2026, 4, 1, 0, 0, 0),
}

_LEAD = {
    "lead_id": "999",
    "nome": "Lead Sem Msg Nova",
    "telefone": "+5500000000000",
    "etapa_id": "1",
}


def _stats_base() -> dict[str, Any]:
    """Mesma forma do dict inicializado em run_daily_inspection."""
    return {
        "total_leads": 0,
        "processed": 0,
        "deep_analyzed": 0,
        "llm_calls": 0,
        "deterministic": 0,
        "skipped_no_phone": 0,
        "skipped_sem_dado": 0,
        "skipped_interno": 0,
        "cache_hits": 0,
        "errors": 0,
        "by_status": {
            "atendido": 0, "nao_atendido": 0, "sem_resposta": 0,
            "atendimento_pendente": 0, "sem_dado_historico": 0, "erro": 0,
        },
        "by_temperatura": {"frio": 0, "morno": 0, "quente": 0},
        "by_mode": {
            "conversa_whatsapp": 0, "tarefa_atrasada": 0,
            "sem_atividade": 0, "sem_dado_historico": 0,
        },
        "tokens_classifier": 0,
        "tokens_analyzer": 0,
        "analyses": [],
    }


def _run(overdue_tasks: list[dict], should_reanalyze_result: bool) -> dict[str, Any]:
    """Roda _process_corretora pra 1 corretora com 1 lead, tudo mockado.

    O Kommo saiu do Inspector em 25/08/2026: `_process_corretora` não recebe
    mais `kommo`, os leads vêm de `get_leads_for_inspector_db` e a lista de
    tarefas atrasadas é `[]` fixo até existir fonte de tarefa viva. O bypass de
    cache por tarefa continua no código — então as tarefas do cenário entram
    pelos dois helpers que o pipeline consulta (as funções reais fazem o
    trabalho; só a lista de entrada é a do teste).
    """
    evolution = MagicMock()
    evolution.check_instance_status.return_value = "open"
    evolution.build_jid_lookup_map.return_value = {}

    stats = _stats_base()

    with (
        patch("app.inspector.pipeline._mark_orphan_runs"),
        patch("app.inspector.pipeline._get_last_run_finished_at", return_value=None),
        patch("app.inspector.pipeline._create_inspector_run", return_value=uuid.uuid4()),
        patch("app.inspector.pipeline._complete_inspector_run"),
        patch("app.inspector.pipeline._upsert_snapshot"),
        patch("app.inspector.pipeline.get_leads_for_inspector_db",
              return_value=[dict(_LEAD)]),
        patch("app.inspector.pipeline._get_lead_tasks_detail",
              side_effect=lambda _vazia, lead_id: _get_lead_tasks_detail(overdue_tasks, lead_id)),
        patch("app.inspector.pipeline._parse_tasks_for_lead",
              side_effect=lambda _vazia, lead_id: _parse_tasks_for_lead(overdue_tasks, lead_id)),
        patch("app.inspector.pipeline.build_cache_maps", return_value=({}, {})),
        patch("app.inspector.pipeline.should_reanalyze", return_value=should_reanalyze_result),
        patch("app.inspector.pipeline.get_conversation_db", return_value=[]),
        patch("app.services.lead_spine.upsert_lead_shadow"),
    ):
        _process_corretora(
            corretora=dict(_CORRETORA),
            stats=stats,
            evolution=evolution,
            stage_map={},
            internos_phones=set(),
            dry_run=True,
        )

    return stats


class TestCacheNaoPulaLeadComTarefaAtrasada:
    def test_lead_com_tarefa_atrasada_nao_e_pulado_pelo_cache(self) -> None:
        """Cache diz "sem msg nova" (should_reanalyze=False), mas o lead TEM
        tarefa atrasada → não pode ser pulado; deve virar análise
        tarefa_atrasada (determinística, zero LLM)."""
        tarefas = [{
            "entity_id": "999", "task_id": 1, "tipo": "Ligar",
            "complete_till_at": "2026-07-01", "dias_atrasada": 3,
        }]
        stats = _run(overdue_tasks=tarefas, should_reanalyze_result=False)

        assert stats["cache_hits"] == 0, (
            "lead com tarefa atrasada foi pulado pelo cache de mensagens"
        )
        assert stats["by_mode"]["tarefa_atrasada"] == 1
        assert stats["processed"] == 1
        assert stats.get("cache_bypass_tarefa_atrasada") == 1
        # zero LLM: modo tarefa_atrasada é determinístico
        assert stats["llm_calls"] == 0

    def test_lead_sem_tarefa_e_sem_msg_nova_continua_pulado_pelo_cache(self) -> None:
        """Guarda de regressão: sem tarefa atrasada, o cache continua
        funcionando normalmente (comportamento pré-fix preservado) — não
        queremos que o bypass vire um "sempre reanalisa tudo" disfarçado."""
        stats = _run(overdue_tasks=[], should_reanalyze_result=False)

        assert stats["cache_hits"] == 1
        assert stats["processed"] == 0
        assert stats["by_mode"]["tarefa_atrasada"] == 0
        assert stats.get("cache_bypass_tarefa_atrasada", 0) == 0

    def test_lead_com_tarefa_atrasada_e_cache_miss_tambem_funciona(self) -> None:
        """Se o cache já mandaria reanalisar (should_reanalyze=True) e o lead
        tem tarefa atrasada, o resultado é o mesmo (tarefa_atrasada) — o
        bypass não deve introduzir um caminho duplo/conflitante."""
        tarefas = [{
            "entity_id": "999", "task_id": 2, "tipo": "Enviar proposta",
            "complete_till_at": "2026-07-05", "dias_atrasada": 1,
        }]
        stats = _run(overdue_tasks=tarefas, should_reanalyze_result=True)

        assert stats["cache_hits"] == 0
        assert stats["by_mode"]["tarefa_atrasada"] == 1
        assert stats["processed"] == 1
