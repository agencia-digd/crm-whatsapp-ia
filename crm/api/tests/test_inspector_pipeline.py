"""
Testes do pipeline — mock de tudo (fonte de leads, Evolution, classifier,
analyzer, DB). Zero chamadas reais.

O Kommo saiu do Inspector em 25/08/2026 (commit f6200c89): os leads vêm de
`fonte_leads.get_leads_for_inspector_db` (banco próprio) e a lista de tarefas
atrasadas do pipeline é VAZIA de propósito, até existir fonte de tarefa viva.
Por isso:
  - a fonte de leads é substituída em `pipeline.get_leads_for_inspector_db`;
  - os testes do modo `tarefa_atrasada` injetam as tarefas pelos dois helpers
    que o pipeline consulta (`_tarefas_injetadas`). O ramo determinístico
    continua no código e continua guardado; o que não existe hoje é quem o
    alimente em produção.
"""
import json
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.inspector.classifier import ClassificationResult
from app.inspector.analyzer import DeepAnalysisResult
from app.inspector.pipeline import (
    LeadAnalysisOutput,
    _apply_connection_window,
    _build_gap_tarefa,
    _build_sugestao_tarefa,
    _cap_temperatura_staleness,
    _detectar_analise_suspeita,
    _get_lead_tasks_detail,
    _infer_temperatura_from_tasks,
    _parse_tasks_for_lead,
    _validar_ultima_msg_meta,
    run_daily_inspection,
)


# ── Fixtures / helpers ────────────────────────────────────────────────────────

def _make_classification(
    status: str = "atendido",
    temperatura: str = "quente",
    deserves: bool = True,
    tokens: int = 100,
) -> ClassificationResult:
    return ClassificationResult(
        status=status,
        temperatura=temperatura,
        deserves_deep_analysis=deserves,
        reason="teste",
        tokens_used=tokens,
    )


def _make_deep(tokens: int = 300) -> DeepAnalysisResult:
    return DeepAnalysisResult(
        conversa_resumo="Lead pediu visita.",
        o_que_corretora_disse="Vou verificar disponibilidade.",
        gap_identificado="Não fechou data da visita.",
        abordagem_sugerida="Propor horário concreto.",
        tokens_used=tokens,
    )


def _make_task_json(lead_id: str, dias: int = 1, tipo: str = "Ligação") -> dict:
    """Cria dict de tarefa no formato que get_overdue_tasks retorna."""
    complete_till = int(time.time()) - dias * 86400
    return {
        "task_id": 1,
        "lead_id": lead_id,
        "entity_id": int(lead_id),
        "entity_type": "leads",
        "tipo": tipo,
        "text": tipo,
        "due_at": complete_till,
        "complete_till_at": complete_till,
        "dias_atrasada": dias,
    }


_CORRETORA_DB = {
    "id": 3,
    "nome": "Pessoa3",
    "evolution_instance": "acme_corretora_a",
    "kommo_user_id": 10000003,
    "evolution_connected_since": datetime(2026, 4, 1, 0, 0, 0),
}

_LEAD_WITH_PHONE = {
    "lead_id": "11111",
    "nome": "João Silva",
    "telefone": "+5500000000000",
    "etapa_id": "10000001",
    "etapa_nome": "Primeiro Contato",
    "origem": "site",
    "criado_em": 1700000000,
    "atualizado_em": 1700000100,
}

_LEAD_NO_PHONE = {
    "lead_id": "22222",
    "nome": "Sem Fone",
    "telefone": "",
    "etapa_id": "10000001",
    "etapa_nome": "Primeiro Contato",
    "origem": "",
    "criado_em": 1700000000,
    "atualizado_em": 1700000100,
}

def _today_iso(hour: int = 9, minute: int = 0) -> str:
    """Timestamp de "hoje" ancorado em São Paulo (não em UTC).

    O pipeline decide sem_atividade_hoje comparando com a data SP
    (`_last_message_is_today_sp`). Usar `date.today()` (UTC no servidor)
    fazia estes testes falharem entre 21h e 23h59 SP, quando a data UTC
    já virou mas a SP não — flakiness por hora do dia, não bug do pipeline.
    """
    from zoneinfo import ZoneInfo
    sp = ZoneInfo("America/Sao_Paulo")
    today_sp = datetime.now(sp).date()
    dt = datetime(today_sp.year, today_sp.month, today_sp.day, hour, minute, tzinfo=sp)
    return dt.isoformat()


_MESSAGES = [
    {"from": "corretora", "text": "Oi!", "at": _today_iso(9, 0), "type": "text"},
    {"from": "lead", "text": "Olá, interesse!", "at": _today_iso(9, 5), "type": "text"},
    {"from": "lead", "text": "Tem visita disponível?", "at": _today_iso(9, 6), "type": "text"},
]

_MESSAGES_OLD = [
    {"from": "corretora", "text": "Oi!", "at": "2026-04-20T09:00:00+00:00", "type": "text"},
    {"from": "lead", "text": "Interesse!", "at": "2026-04-20T09:01:00+00:00", "type": "text"},
]


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


def _tarefas_injetadas(tarefas: list[dict]):
    """Faz o pipeline enxergar `tarefas` como as tarefas atrasadas da rodada.

    Em produção `overdue_tasks` é `[]` fixo desde 25/08/2026 (o Kommo, que era
    a fonte, foi abandonado pela operação). Os dois helpers abaixo são o único
    ponto em que o pipeline lê essa lista — substituí-los por versões que leem
    `tarefas` exercita o ramo `tarefa_atrasada` e o bypass de cache sem mexer
    no código de produção. As funções REAIS fazem o trabalho (importadas no
    topo, antes do patch); só a lista de entrada muda.
    """
    return (
        patch("app.inspector.pipeline._get_lead_tasks_detail",
              side_effect=lambda _vazia, lead_id: _get_lead_tasks_detail(tarefas, lead_id)),
        patch("app.inspector.pipeline._parse_tasks_for_lead",
              side_effect=lambda _vazia, lead_id: _parse_tasks_for_lead(tarefas, lead_id)),
    )


# ── _apply_connection_window ──────────────────────────────────────────────────

class TestApplyConnectionWindow:
    def test_sem_connected_since_retorna_tudo(self) -> None:
        msgs = [{"at": "2026-05-01T10:00:00+00:00", "from": "lead", "text": "oi", "type": "text"}]
        assert _apply_connection_window(msgs, None) == msgs

    def test_filtra_msgs_anteriores(self) -> None:
        connected = datetime(2026, 5, 4, 0, 0, 0)
        msgs = [
            {"at": "2026-04-20T10:00:00+00:00", "from": "corretora", "text": "a", "type": "text"},
            {"at": "2026-05-05T10:00:00+00:00", "from": "lead", "text": "b", "type": "text"},
        ]
        result = _apply_connection_window(msgs, connected)
        assert len(result) == 1
        assert result[0]["from"] == "lead"

    def test_msg_exatamente_na_conexao_incluida(self) -> None:
        connected = datetime(2026, 5, 4, 10, 0, 0)
        msgs = [{"at": "2026-05-04T10:00:00+00:00", "from": "corretora", "text": "x", "type": "text"}]
        assert len(_apply_connection_window(msgs, connected)) == 1

    def test_sem_at_field_ignorado(self) -> None:
        connected = datetime(2026, 5, 1, 0, 0, 0)
        msgs = [{"at": "", "from": "lead", "text": "y", "type": "text"}]
        assert _apply_connection_window(msgs, connected) == []


# ── Helpers de modo tarefa_atrasada ──────────────────────────────────────────

class TestInferTemperatura:
    def test_1_dia_morno(self) -> None:
        detail = [{"tipo": "Ligação", "vencida_em": "", "dias_atrasada": 1}]
        assert _infer_temperatura_from_tasks(detail) == "morno"

    def test_2_dias_morno(self) -> None:
        detail = [{"tipo": "Ligação", "vencida_em": "", "dias_atrasada": 2}]
        assert _infer_temperatura_from_tasks(detail) == "morno"

    def test_3_dias_frio(self) -> None:
        detail = [{"tipo": "Ligação", "vencida_em": "", "dias_atrasada": 3}]
        assert _infer_temperatura_from_tasks(detail) == "frio"

    def test_4_dias_frio(self) -> None:
        detail = [{"tipo": "Ligação", "vencida_em": "", "dias_atrasada": 4}]
        assert _infer_temperatura_from_tasks(detail) == "frio"

    def test_7_dias_frio(self) -> None:
        detail = [{"tipo": "Ligação", "vencida_em": "", "dias_atrasada": 7}]
        assert _infer_temperatura_from_tasks(detail) == "frio"

    def test_vazia_frio(self) -> None:
        assert _infer_temperatura_from_tasks([]) == "frio"

    def test_multiplas_usa_pior(self) -> None:
        detail = [
            {"tipo": "Ligação", "vencida_em": "", "dias_atrasada": 1},
            {"tipo": "E-mail", "vencida_em": "", "dias_atrasada": 5},
        ]
        assert _infer_temperatura_from_tasks(detail) == "frio"


class TestBuildSugestaoTarefa:
    def test_1_dia_sem_urgencia(self) -> None:
        detail = [{"tipo": "Ligação 1", "vencida_em": "", "dias_atrasada": 1}]
        result = _build_sugestao_tarefa("Pessoa", detail)
        assert "1 tarefa" in result
        assert "1 dia" in result
        assert "⚠️" not in result

    def test_6_dias_com_urgencia(self) -> None:
        detail = [{"tipo": "Follow-up", "vencida_em": "", "dias_atrasada": 6}]
        result = _build_sugestao_tarefa("João", detail)
        assert "⚠️" in result or "URGENTE" in result

    def test_gap_menciona_dias(self) -> None:
        detail = [{"tipo": "Ligação", "vencida_em": "", "dias_atrasada": 3}]
        gap = _build_gap_tarefa(detail)
        assert "3 dia" in gap
        assert "follow-up" in gap.lower() or "vencida" in gap.lower()


class TestGetLeadTasksDetail:
    def test_filtra_pelo_lead_id(self) -> None:
        tasks = [
            _make_task_json("11111", dias=2),
            _make_task_json("99999", dias=5),
        ]
        result = _get_lead_tasks_detail(tasks, "11111")
        assert len(result) == 1
        assert result[0]["dias_atrasada"] == 2

    def test_lead_sem_tarefa_retorna_vazio(self) -> None:
        tasks = [_make_task_json("99999", dias=1)]
        assert _get_lead_tasks_detail(tasks, "11111") == []

    def test_multiplas_tarefas_mesmo_lead(self) -> None:
        task1 = _make_task_json("11111", dias=1)
        task2 = {**_make_task_json("11111", dias=3), "task_id": 2}
        result = _get_lead_tasks_detail([task1, task2], "11111")
        assert len(result) == 2


# ── run_daily_inspection ──────────────────────────────────────────────────────

class TestRunDailyInspection:
    def _run(
        self,
        leads: list,
        messages: list,
        classification: ClassificationResult | None = None,
        deep: DeepAnalysisResult | None = None,
        corretora: dict | None = None,
        overdue_tasks: str = "[]",
    ) -> dict:
        c = corretora or _CORRETORA_DB
        mock_session = _mock_db_session(c)
        detalhe, textos = _tarefas_injetadas(json.loads(overdue_tasks))

        with (
            patch("app.inspector.pipeline.SessionLocal", return_value=mock_session),
            patch("app.inspector.pipeline.get_leads_for_inspector_db", return_value=leads),
            detalhe,
            textos,
            patch("app.inspector.pipeline.EvolutionTool"),
            patch("app.inspector.pipeline.get_conversation_db", return_value=messages),
            patch("app.inspector.pipeline.classify_lead", return_value=classification or _make_classification()),
            patch("app.inspector.pipeline.analyze_lead", return_value=deep),
            patch("app.inspector.pipeline._upsert_snapshot"),
            patch("app.inspector.pipeline._save_analysis"),
            patch("app.inspector.pipeline._mark_orphan_runs"),
            patch("app.inspector.pipeline._get_last_run_finished_at", return_value=None),
            patch("app.inspector.pipeline._create_inspector_run", return_value=uuid.uuid4()),
            patch("app.inspector.pipeline._complete_inspector_run"),
            patch("app.inspector.pipeline._fail_inspector_run"),
        ):
            return run_daily_inspection(only_corretora_id=3, dry_run=True)

    # ── Modo conversa_whatsapp ────────────────────────────────────────────────

    def test_lead_com_conversa_whatsapp_modo_conversa(self) -> None:
        stats = self._run(
            leads=[_LEAD_WITH_PHONE],
            messages=_MESSAGES,
            classification=_make_classification("atendido", "quente", True),
            deep=_make_deep(),
        )
        assert stats["by_mode"]["conversa_whatsapp"] == 1
        assert stats["by_mode"]["tarefa_atrasada"] == 0
        analysis = stats["analyses"][0]
        assert analysis["analysis_mode"] == "conversa_whatsapp"
        assert stats["llm_calls"] == 1

    def test_1_lead_com_conversa_gera_analysis(self) -> None:
        stats = self._run(
            leads=[_LEAD_WITH_PHONE],
            messages=_MESSAGES,
            classification=_make_classification("atendido", "quente", True),
            deep=_make_deep(),
        )
        assert stats["total_leads"] == 1
        assert stats["processed"] == 1
        assert stats["deep_analyzed"] == 1
        assert stats["by_status"]["atendido"] == 1
        assert stats["by_temperatura"]["quente"] == 1

    # ── Modo tarefa_atrasada ──────────────────────────────────────────────────

    def test_lead_sem_conversa_com_tarefa_atrasada_modo_tarefa(self) -> None:
        task = _make_task_json("11111", dias=2)
        stats = self._run(
            leads=[_LEAD_WITH_PHONE],
            messages=[],
            overdue_tasks=json.dumps([task]),
        )
        assert stats["by_mode"]["tarefa_atrasada"] == 1
        assert stats["llm_calls"] == 0
        assert stats["deterministic"] == 1
        analysis = stats["analyses"][0]
        assert analysis["analysis_mode"] == "tarefa_atrasada"
        assert analysis["status"] == "atendimento_pendente"
        assert analysis["gap_identificado"] is not None
        assert analysis["abordagem_sugerida"] is not None

    def test_temperatura_inferida_1_dia_morno(self) -> None:
        task = _make_task_json("11111", dias=1)
        stats = self._run(
            leads=[_LEAD_WITH_PHONE],
            messages=[],
            overdue_tasks=json.dumps([task]),
        )
        analysis = stats["analyses"][0]
        assert analysis["temperatura"] == "morno"

    def test_temperatura_inferida_4_dias_frio(self) -> None:
        task = _make_task_json("11111", dias=4)
        stats = self._run(
            leads=[_LEAD_WITH_PHONE],
            messages=[],
            overdue_tasks=json.dumps([task]),
        )
        analysis = stats["analyses"][0]
        assert analysis["temperatura"] == "frio"

    def test_temperatura_inferida_7_dias_frio_urgente(self) -> None:
        task = _make_task_json("11111", dias=7)
        stats = self._run(
            leads=[_LEAD_WITH_PHONE],
            messages=[],
            overdue_tasks=json.dumps([task]),
        )
        analysis = stats["analyses"][0]
        assert analysis["temperatura"] == "frio"
        assert "URGENTE" in (analysis.get("abordagem_sugerida") or "")

    def test_abordagem_sugerida_deterministica_para_tarefa_atrasada(self) -> None:
        task = _make_task_json("11111", dias=3, tipo="Ligação 1 - dia 2 do FUP")
        stats = self._run(
            leads=[_LEAD_WITH_PHONE],
            messages=[],
            overdue_tasks=json.dumps([task]),
        )
        analysis = stats["analyses"][0]
        assert "tarefa" in (analysis.get("abordagem_sugerida") or "").lower()
        assert "3 dia" in (analysis.get("abordagem_sugerida") or "")

    # ── Modo sem_atividade ────────────────────────────────────────────────────

    def test_lead_sem_conversa_sem_tarefa_modo_sem_atividade(self) -> None:
        stats = self._run(
            leads=[_LEAD_WITH_PHONE],
            messages=[],
            overdue_tasks="[]",
        )
        assert stats["by_mode"]["sem_atividade"] == 1
        assert stats["llm_calls"] == 0
        analysis = stats["analyses"][0]
        assert analysis["analysis_mode"] == "sem_atividade"
        assert analysis["status"] == "nao_atendido"

    # ── Casos existentes mantidos ─────────────────────────────────────────────

    def test_lead_sem_telefone_pulado_com_warn(self) -> None:
        stats = self._run(leads=[_LEAD_NO_PHONE], messages=[])
        assert stats["skipped_no_phone"] == 1
        assert stats["processed"] == 1
        analysis = stats["analyses"][0]
        assert analysis["skipped_reason"] == "no_phone"
        assert analysis["analysis_mode"] == "sem_atividade"

    def test_lead_anterior_conexao_pulado_silenciosamente(self) -> None:
        corretora_tardia = {**_CORRETORA_DB, "evolution_connected_since": datetime(2026, 5, 4, 0, 0, 0)}
        stats = self._run(
            leads=[_LEAD_WITH_PHONE],
            messages=_MESSAGES_OLD,
            corretora=corretora_tardia,
        )
        assert stats["skipped_sem_dado"] == 1
        analysis = stats["analyses"][0]
        assert analysis["status"] == "sem_dado_historico"
        assert analysis["analysis_mode"] == "sem_dado_historico"
        assert analysis["skipped_reason"] == "evolution_connected_after_lead_activity"

    def test_lead_misto_so_analisa_msgs_pos_conexao(self) -> None:
        corretora = {**_CORRETORA_DB, "evolution_connected_since": datetime(2026, 4, 2, 9, 3, 0)}
        stats = self._run(
            leads=[_LEAD_WITH_PHONE],
            messages=_MESSAGES,
            corretora=corretora,
            classification=_make_classification("atendido", "quente", False),
        )
        assert stats["processed"] == 1
        assert stats["by_mode"]["conversa_whatsapp"] == 1

    def test_erro_llm_grava_status_erro(self) -> None:
        with (
            patch("app.inspector.pipeline.SessionLocal", return_value=_mock_db_session(_CORRETORA_DB)),
            patch("app.inspector.pipeline.get_leads_for_inspector_db", return_value=[_LEAD_WITH_PHONE]),
            patch("app.inspector.pipeline.EvolutionTool"),
            patch("app.inspector.pipeline.get_conversation_db", return_value=_MESSAGES),
            patch("app.inspector.pipeline.classify_lead", side_effect=Exception("LLM down")),
            patch("app.inspector.pipeline._upsert_snapshot"),
            patch("app.inspector.pipeline._save_analysis"),
            patch("app.inspector.pipeline._mark_orphan_runs"),
            patch("app.inspector.pipeline._get_last_run_finished_at", return_value=None),
            patch("app.inspector.pipeline._create_inspector_run", return_value=uuid.uuid4()),
            patch("app.inspector.pipeline._complete_inspector_run"),
            patch("app.inspector.pipeline._fail_inspector_run"),
        ):
            stats = run_daily_inspection(only_corretora_id=3, dry_run=True)

        assert stats["errors"] == 1
        assert stats["by_status"]["erro"] == 1

    def test_dry_run_nao_chama_save_analysis(self) -> None:
        with (
            patch("app.inspector.pipeline.SessionLocal", return_value=_mock_db_session(_CORRETORA_DB)),
            patch("app.inspector.pipeline.get_leads_for_inspector_db", return_value=[_LEAD_WITH_PHONE]),
            patch("app.inspector.pipeline.EvolutionTool"),
            patch("app.inspector.pipeline.get_conversation_db", return_value=_MESSAGES),
            patch("app.inspector.pipeline.classify_lead", return_value=_make_classification()),
            patch("app.inspector.pipeline.analyze_lead", return_value=None),
            patch("app.inspector.pipeline._upsert_snapshot"),
            patch("app.inspector.pipeline._save_analysis") as mock_save,
            patch("app.inspector.pipeline._mark_orphan_runs"),
            patch("app.inspector.pipeline._get_last_run_finished_at", return_value=None),
            patch("app.inspector.pipeline._create_inspector_run", return_value=uuid.uuid4()),
            patch("app.inspector.pipeline._complete_inspector_run"),
            patch("app.inspector.pipeline._fail_inspector_run"),
        ):
            run_daily_inspection(only_corretora_id=3, dry_run=True)

        mock_save.assert_not_called()

    def test_tokens_acumulados_corretamente(self) -> None:
        stats = self._run(
            leads=[_LEAD_WITH_PHONE],
            messages=_MESSAGES,
            classification=_make_classification(tokens=120),
            deep=_make_deep(tokens=350),
        )
        assert stats["tokens_classifier"] == 120
        assert stats["tokens_analyzer"] == 350
        assert stats["tokens"]["total"] == 470

    def test_by_mode_acumulado_corretamente(self) -> None:
        """Dois leads: um com conversa, um sem conversa mas com tarefa."""
        lead2 = {**_LEAD_WITH_PHONE, "lead_id": "22222", "telefone": "+000000000000000"}
        task = _make_task_json("22222", dias=2)

        # Lead 1 tem mensagens, Lead 2 não tem
        def fake_conversation(
            corretora_id: int, phone: str, instance: str, evolution: object,
            hours_back: int = 72, cutoff_dt: object = None,
        ) -> list:
            return _MESSAGES if phone == _LEAD_WITH_PHONE["telefone"] else []

        detalhe, textos = _tarefas_injetadas([task])
        with (
            patch("app.inspector.pipeline.SessionLocal", return_value=_mock_db_session(_CORRETORA_DB)),
            patch("app.inspector.pipeline.get_leads_for_inspector_db", return_value=[_LEAD_WITH_PHONE, lead2]),
            detalhe,
            textos,
            patch("app.inspector.pipeline.EvolutionTool"),
            patch("app.inspector.pipeline.get_conversation_db", side_effect=fake_conversation),
            patch("app.inspector.pipeline.classify_lead", return_value=_make_classification()),
            patch("app.inspector.pipeline.analyze_lead", return_value=None),
            patch("app.inspector.pipeline._upsert_snapshot"),
            patch("app.inspector.pipeline._save_analysis"),
            patch("app.inspector.pipeline._mark_orphan_runs"),
            patch("app.inspector.pipeline._get_last_run_finished_at", return_value=None),
            patch("app.inspector.pipeline._create_inspector_run", return_value=uuid.uuid4()),
            patch("app.inspector.pipeline._complete_inspector_run"),
            patch("app.inspector.pipeline._fail_inspector_run"),
        ):
            stats = run_daily_inspection(only_corretora_id=3, dry_run=True)

        assert stats["by_mode"]["conversa_whatsapp"] == 1
        assert stats["by_mode"]["tarefa_atrasada"] == 1


# ── Canal 'cloud' (Pessoa12): lê da tabela, pula a instância Evolution ────────────────

_CORRETORA_CLOUD = {**_CORRETORA_DB, "canal": "cloud"}


class TestCanalCloud:
    """A Pessoa12 (canal='cloud') lê a conversa da tabela mensagens_whatsapp via
    ler_thread_cloud; o Inspector NÃO bate na instância Evolution (nem
    check_instance_status, nem build_jid_lookup_map, nem get_conversation_db) e
    NÃO registra pipeline_instancia_offline."""

    def _run_cloud(self, leads: list, messages: list) -> tuple[dict, MagicMock]:
        with (
            patch("app.inspector.pipeline.SessionLocal", return_value=_mock_db_session(_CORRETORA_CLOUD)),
            patch("app.inspector.pipeline.get_leads_for_inspector_db", return_value=leads),
            patch("app.inspector.pipeline.EvolutionTool") as MockEvolution,
            patch("app.inspector.pipeline._ler_conversa_cloud", return_value=messages) as mock_cloud,
            patch("app.inspector.pipeline.get_conversation_db") as mock_evo_read,
            patch("app.inspector.pipeline.classify_lead", return_value=_make_classification("atendido", "quente", True)),
            patch("app.inspector.pipeline.analyze_lead", return_value=_make_deep()),
            patch("app.inspector.pipeline._upsert_snapshot"),
            patch("app.inspector.pipeline._save_analysis"),
            patch("app.inspector.pipeline._mark_orphan_runs"),
            patch("app.inspector.pipeline._get_last_run_finished_at", return_value=None),
            patch("app.inspector.pipeline._create_inspector_run", return_value=uuid.uuid4()),
            patch("app.inspector.pipeline._complete_inspector_run"),
            patch("app.inspector.pipeline._fail_inspector_run"),
        ):
            stats = run_daily_inspection(only_corretora_id=3, dry_run=True)
            evo_inst = MockEvolution.return_value
            return stats, (evo_inst, mock_cloud, mock_evo_read)

    def test_corretor_le_da_tabela_e_gera_analysis(self) -> None:
        stats, (evo_inst, mock_cloud, mock_evo_read) = self._run_cloud(
            leads=[_LEAD_WITH_PHONE], messages=_MESSAGES,
        )
        # Leu a conversa da tabela (cloud), não do Evolution.
        mock_cloud.assert_called_once()
        mock_evo_read.assert_not_called()
        # Gerou análise de conversa pra Pessoa12.
        assert stats["by_mode"]["conversa_whatsapp"] == 1
        assert stats["analyses"][0]["corretora"] == "Pessoa3"
        assert stats["analyses"][0]["analysis_mode"] == "conversa_whatsapp"

    def test_corretor_nao_bate_na_instancia_evolution(self) -> None:
        stats, (evo_inst, _cloud, _evo_read) = self._run_cloud(
            leads=[_LEAD_WITH_PHONE], messages=_MESSAGES,
        )
        # NÃO checou status da instância nem montou jid_map (instância fica close).
        evo_inst.check_instance_status.assert_not_called()
        evo_inst.build_jid_lookup_map.assert_not_called()
        # E NÃO marcou a Pessoa12 como offline.
        assert stats.get("instancias_offline", []) == []


# ── I4.2 / I6.2 / I6.3: guardrails de staleness e coerência (funções puras) ─────

class _FakeDeep:
    """Stub mínimo de DeepAnalysisResult pra coerência (só precisa de conversa_resumo)."""
    def __init__(self, conversa_resumo: str | None) -> None:
        self.conversa_resumo = conversa_resumo


class TestCapTemperaturaStaleness:
    def test_quente_com_silencio_moderado_vira_morno(self) -> None:
        assert _cap_temperatura_staleness("quente", 20) == ("morno", True)

    def test_qualquer_com_silencio_alto_vira_frio(self) -> None:
        assert _cap_temperatura_staleness("quente", 40) == ("frio", True)
        assert _cap_temperatura_staleness("morno", 40) == ("frio", True)

    def test_silencio_baixo_nao_rebaixa(self) -> None:
        assert _cap_temperatura_staleness("quente", 5) == ("quente", False)
        assert _cap_temperatura_staleness("morno", 10) == ("morno", False)

    def test_frio_permanece_frio(self) -> None:
        assert _cap_temperatura_staleness("frio", 50) == ("frio", False)

    def test_dias_none_nao_rebaixa(self) -> None:
        assert _cap_temperatura_staleness("quente", None) == ("quente", False)


class TestDetectarAnaliseSuspeita:
    _CONV_COM_LEAD = [
        {"from": "lead", "text": "Quero visitar", "at": "2026-05-01T09:00:00+00:00", "type": "text"},
        {"from": "corretora", "text": "Claro!", "at": "2026-05-01T09:01:00+00:00", "type": "text"},
    ]
    _CONV_SO_CORRETORA = [
        {"from": "corretora", "text": "Oi", "at": "2026-05-01T09:00:00+00:00", "type": "text"},
    ]

    def test_classifier_diz_sem_resposta_mas_deep_tem_dialogo(self) -> None:
        # o bug I1 (conversa trocada) seria pego aqui
        deep = _FakeDeep("Lead conversou longamente sobre financiamento.")
        assert _detectar_analise_suspeita(
            "Lead não respondeu à corretora", self._CONV_COM_LEAD, deep
        ) is True

    def test_deep_presente_mas_zero_msg_do_lead(self) -> None:
        deep = _FakeDeep("Resumo detalhado do diálogo.")
        assert _detectar_analise_suspeita(
            "Conversa boa", self._CONV_SO_CORRETORA, deep
        ) is True

    def test_coerente_nao_marca(self) -> None:
        deep = _FakeDeep("Resumo.")
        assert _detectar_analise_suspeita(
            "Lead engajou e fez perguntas", self._CONV_COM_LEAD, deep
        ) is False

    def test_sem_deep_nao_marca(self) -> None:
        assert _detectar_analise_suspeita(
            "Lead não respondeu", self._CONV_COM_LEAD, None
        ) is False


class TestValidarUltimaMsgMeta:
    _CONV = [
        {"from": "lead", "text": "oi", "at": "2026-05-01T09:00:00+00:00", "type": "text"},
        {"from": "corretora", "text": "olá", "at": "2026-05-01T09:05:00+00:00", "type": "text"},
    ]

    def test_meta_correta_nao_diverge(self) -> None:
        at, de, div = _validar_ultima_msg_meta(
            "2026-05-01T09:05:00+00:00", "corretora", self._CONV
        )
        assert div is False
        assert de == "corretora"

    def test_meta_errada_e_corrigida(self) -> None:
        # passa metadados errados (lead) → função corrige pra corretora e sinaliza divergência
        at, de, div = _validar_ultima_msg_meta(
            "2026-05-01T09:00:00+00:00", "lead", self._CONV
        )
        assert div is True
        assert de == "corretora"
        assert at == "2026-05-01T09:05:00+00:00"


# ── Multi-linha: a conversa lida tem que ser a DA CORRETORA ────────────────────

class TestCanalCloudMultiLinha:
    """Pessoa10, Pessoa11 e Corretor também são canal='cloud'. `ler_thread_cloud` filtra por
    corretora_id e cai no default da Pessoa12 (3) quando ninguém passa — o que devolvia
    thread VAZIA pras outras três e deixava o Inspector analisando lead sem
    conversa nenhuma (605 análises em 30h, zero com resumo de conversa)."""

    def test_ler_conversa_cloud_passa_corretora_id(self) -> None:
        from app.inspector import pipeline as pl

        with patch("app.api.cloud_corretora_a.ler_thread_cloud", return_value=[]) as mock_thread:
            pl._ler_conversa_cloud("+5500000000000", corretora_id=1)

        _args, kwargs = mock_thread.call_args
        assert kwargs.get("corretora_id") == 1, (
            "sem corretora_id a thread volta vazia pra toda corretora que não é a Pessoa12"
        )


# ── Porta do Haiku removida (26/08/2026) ──────────────────────────────────────
#
# Antes: `deserves_deep_analysis=False` impedia o Sonnet de analisar o lead, e
# ele sumia do briefing das 20h, do coaching da corretora e do radar da Dona.
# Medido em 26/08: 40 leads COM conversa foram descartados assim em 7 dias,
# num volume de só 43-94 leads/dia — economia que não se justificava.
#
# Agora: todo lead com conversa é analisado. O classifier segue rodando (a
# temperatura tem 12 consumidores), mas o veredito dele vira registro, não porta.
#
# Helper próprio (sem tarefas injetadas): mocka a fonte atual de leads,
# `fonte_leads.get_leads_for_inspector_db`, como o resto do arquivo passou a
# fazer em 05/10/2026.

class TestPortaRemovida:
    def _run(
        self,
        leads: list,
        classification: ClassificationResult,
        deep: DeepAnalysisResult | None = None,
        messages: list | None = None,
    ) -> dict:
        mock_session = _mock_db_session(_CORRETORA_DB)
        with (
            patch("app.inspector.pipeline.SessionLocal", return_value=mock_session),
            patch("app.inspector.pipeline.EvolutionTool"),
            patch("app.inspector.pipeline.get_leads_for_inspector_db", return_value=leads),
            patch("app.inspector.pipeline.get_conversation_db", return_value=messages or _MESSAGES),
            patch("app.inspector.pipeline.classify_lead", return_value=classification),
            patch("app.inspector.pipeline.analyze_lead", return_value=deep),
            patch("app.inspector.pipeline._upsert_snapshot"),
            patch("app.inspector.pipeline._save_analysis"),
            patch("app.inspector.pipeline._mark_orphan_runs"),
            patch("app.inspector.pipeline._get_last_run_finished_at", return_value=None),
            patch("app.inspector.pipeline._create_inspector_run", return_value=uuid.uuid4()),
            patch("app.inspector.pipeline._complete_inspector_run"),
            patch("app.inspector.pipeline._fail_inspector_run"),
        ):
            return run_daily_inspection(only_corretora_id=3, dry_run=True)

    def test_lead_reprovado_pelo_classifier_AGORA_e_analisado(self) -> None:
        """O caso que motivou a mudança: frio + deserves=False era descartado."""
        stats = self._run(
            leads=[_LEAD_WITH_PHONE],
            classification=_make_classification("atendido", "frio", deserves=False),
            deep=_make_deep(),
        )
        assert stats["deep_analyzed"] == 1, "lead com conversa não pode ser descartado"

    def test_registra_que_o_classifier_teria_descartado(self) -> None:
        """Sem esse registro não dá pra provar depois se o Haiku errava."""
        stats = self._run(
            leads=[_LEAD_WITH_PHONE],
            classification=_make_classification("atendido", "frio", deserves=False),
            deep=_make_deep(),
        )
        assert stats["analyses"][0]["classifier_teria_descartado"] is True

    def test_nao_marca_descarte_quando_o_classifier_aprovaria(self) -> None:
        stats = self._run(
            leads=[_LEAD_WITH_PHONE],
            classification=_make_classification("atendido", "quente", deserves=True),
            deep=_make_deep(),
        )
        assert stats["analyses"][0]["classifier_teria_descartado"] is False

    def test_temperatura_continua_vindo_do_classifier(self) -> None:
        """12 consumidores dependem dela — painel, briefing, Assistente, métricas."""
        stats = self._run(
            leads=[_LEAD_WITH_PHONE],
            classification=_make_classification("atendido", "frio", deserves=False),
            deep=_make_deep(),
        )
        assert stats["analyses"][0]["temperatura"] == "frio"

    def test_teto_de_analises_para_o_sonnet_e_registra(self) -> None:
        """Trava contra importação em massa: para no teto, não roda Sonnet 2000x."""
        from app.inspector import pipeline as pl

        leads = []
        for i in range(5):
            lead = dict(_LEAD_WITH_PHONE)
            lead["lead_id"] = f"lead-{i}"
            leads.append(lead)

        with patch.object(pl, "MAX_DEEP_ANALISES_POR_RODADA", 2):
            stats = self._run(
                leads=leads,
                classification=_make_classification("atendido", "quente", deserves=True),
                deep=_make_deep(),
            )

        assert stats["deep_analyzed"] == 2, "teto não segurou"
        assert stats["deep_teto_atingido"] is True
        # os leads acima do teto continuam no relatório, só sem análise profunda
        assert len(stats["analyses"]) == 5
