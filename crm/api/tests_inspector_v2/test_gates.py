from __future__ import annotations

import ast
import json
from datetime import datetime
from pathlib import Path

import pytest

from app.inspector_v2.chunking import chunk_messages, unique_processed_count
from app.inspector_v2.corpus import deterministic_skip_reason, parse_messages
from app.inspector_v2.engine import InspectorV2Engine, _claims_executed_action
from app.inspector_v2.evaluation import reconcile_reference, replay_gate_ok
from app.inspector_v2.models import SelectionItem, SelectionManifest
from app.inspector_v2.models import ProviderResponse
from app.inspector_v2.preparation import assert_safe, prepare, redact_text
from app.inspector_v2.runner import assert_no_lookahead, replay_cutoffs, run_selection, truncate_for_cutoff
from app.inspector_v2.storage import atomic_write_json
from conftest import FakeProvider


def _selection(leads, fingerprint="frozen"):
    return SelectionManifest(
        seed="test",
        corpus_fingerprint=fingerprint,
        target_size=len(leads),
        items=[
            SelectionItem(
                lead_alias=lead.lead_alias,
                corretora_alias=lead.corretora_alias,
                faixa_jornada=lead.faixa_jornada,
                temperatura_referencia=lead.temperatura_referencia,
                mensagens_total=lead.mensagens_total,
                redactions_count=lead.redactions_count,
            )
            for lead in leads
        ],
    )


def test_gate_1_redaction_removes_pii_and_keeps_money() -> None:
    source = (
        "Nome: Pessoa da Silva\nCPF: 123.456.789-01\nCEP: 18060-545\n"
        "Data de nascimento: 11/05/1978\ne-mail: contato@example.com\nRenda R$ 8.500"
    )
    clean, count = redact_text(source, "Pessoa da Silva", "+5500000000000")
    assert count >= 5
    assert "123.456" not in clean and "18060" not in clean and "11/05/1978" not in clean
    assert "contato@example.com" not in clean
    assert "R$ 8.500" in clean


def test_gate_2_no_llm_gate_every_eligible_lead_runs(lead_factory, temp_store) -> None:
    leads = [lead_factory(alias_suffix=letter * 10) for letter in ("C", "D", "E")]
    provider = FakeProvider()
    manifest = run_selection(
        leads,
        "frozen",
        _selection(leads),
        InspectorV2Engine(provider, temp_store, max_chunk_characters=50_000),
        temp_store,
        run_id="no-gate",
    )
    assert manifest.completed == 3 and manifest.failed == 0 and manifest.skipped == 0
    assert sum("MAP parcial" in call for call in provider.calls) == 3
    assert deterministic_skip_reason(leads[0].model_dump()) is None


def test_gate_3_more_than_200_messages_full_coverage_and_order(lead_factory) -> None:
    lead = lead_factory(260)
    messages = parse_messages(lead.mensagens)
    chunks = chunk_messages(messages, max_characters=1_200, overlap_messages=2)
    assert unique_processed_count(chunks) == 260
    assert chunks[-1].descriptor.last_message == 260
    assert any(chunk.descriptor.overlap_before == 2 for chunk in chunks[1:])
    assert [message.timestamp for message in messages] == sorted(message.timestamp for message in messages)


def test_gate_3_single_chunk_still_gets_final_reduce(lead_factory, temp_store) -> None:
    provider = FakeProvider()
    engine = InspectorV2Engine(provider, temp_store, max_chunk_characters=100_000)
    result = engine.analyze(lead_factory(3), "single-reduce")
    assert result.cobertura == 1.0
    assert any("REDUCE FINAL obrigatório" in call for call in provider.calls)


def test_gate_4_existing_audio_transcription_is_preserved() -> None:
    raw = "[2026-08-01 10:00] LEAD [audio]: texto transcrito real"
    parsed = parse_messages([raw])[0]
    assert parsed.raw == raw and parsed.message_type == "audio" and not parsed.automated
    template = parse_messages(["[2026-08-01 10:01] CORRETORA [template]: automático"])[0]
    assert template.automated is True


def test_gate_5_grounding_cannot_pass_without_three_citations(lead_factory, temp_store) -> None:
    provider = FakeProvider(no_citations=True)
    result = InspectorV2Engine(provider, temp_store).analyze(
        lead_factory(3), "no-grounding"
    )
    assert result.checks.citacoes_total == 0
    assert result.checks.citacoes_existentes is False
    assert result.checks.falha_critica is True
    assert "minItems" not in provider.schemas[0]["properties"]["citacoes"]
    assert provider.schemas[-1]["properties"]["citacoes"]["minItems"] == 3


def test_grounding_metric_counts_a_lead_without_citations_as_zero(lead_factory, temp_store) -> None:
    from app.inspector_v2.evaluation import _grounding_score

    grounded = InspectorV2Engine(FakeProvider(), temp_store).analyze(
        lead_factory(3, "GROUNDOKAA"), "grounding-metric-ok"
    )
    missing = InspectorV2Engine(FakeProvider(no_citations=True), temp_store).analyze(
        lead_factory(3, "GROUNDZERO"), "grounding-metric-zero"
    )

    assert grounded.checks.citacoes_validas == grounded.checks.citacoes_total
    assert grounded.checks.citacoes_total >= 3
    assert missing.checks.citacoes_total == 0
    assert _grounding_score([grounded, missing]) == 0.5


def test_gate_5_one_grounding_repair_requires_contiguous_literal_quote(
    lead_factory, temp_store
) -> None:
    class RepairingProvider(FakeProvider):
        def complete(self, system, user, schema):
            response = super().complete(system, user, schema)
            payload = json.loads(response.content)
            if "CORREÇÃO ÚNICA DE GROUNDING" not in user:
                payload["citacoes"][0]["trecho"] = "mensagem palavras 1"
            return ProviderResponse(content=json.dumps(payload), usage=response.usage)

    provider = RepairingProvider()
    result = InspectorV2Engine(provider, temp_store).analyze(lead_factory(3), "grounding-repair")
    assert result.parse_attempts == 2
    assert result.checks.citacoes_existentes is True
    assert result.checks.falha_critica is False
    assert any("CORREÇÃO ÚNICA DE GROUNDING" in call for call in provider.calls)


def test_gate_5_recommendation_is_allowed_but_claim_of_execution_is_not() -> None:
    assert _claims_executed_action("Enviar WhatsApp após revisão humana.") is False
    assert _claims_executed_action("Mover no CRM se a corretora aprovar.") is False
    assert _claims_executed_action("Retomar a conversa sem alterar o briefing.") is False
    assert _claims_executed_action("Já movi o lead no CRM.") is True
    assert _claims_executed_action("WhatsApp enviado e tarefa concluída.") is True


def test_gate_5_human_review_annotation_does_not_count_as_side_effect(
    lead_factory, temp_store
) -> None:
    class RecommendationProvider(FakeProvider):
        def __init__(self, executed: bool) -> None:
            super().__init__()
            self.executed = executed

        def complete(self, system, user, schema):
            response = super().complete(system, user, schema)
            payload = json.loads(response.content)
            payload["proxima_acao"] = "Enviar WhatsApp após revisão humana."
            payload["acao_externa_executada"] = self.executed
            return ProviderResponse(content=json.dumps(payload), usage=response.usage)

    safe = InspectorV2Engine(RecommendationProvider(False), temp_store).analyze(
        lead_factory(3), "human-review-safe"
    )
    assert safe.checks.nenhuma_acao_operacional is True
    assert safe.checks.falha_critica is False

    unsafe = InspectorV2Engine(RecommendationProvider(True), temp_store).analyze(
        lead_factory(3), "human-review-unsafe"
    )
    assert unsafe.checks.nenhuma_acao_operacional is False
    assert unsafe.checks.falha_critica is True


def test_gate_6_idempotent_and_invalid_json_resumable(lead_factory, temp_store) -> None:
    lead = lead_factory(3)
    bad_manifest = run_selection(
        [lead], "frozen", _selection([lead]),
        InspectorV2Engine(FakeProvider(invalid=True), temp_store), temp_store, run_id="resume",
    )
    assert bad_manifest.failed == 1
    good_engine = InspectorV2Engine(FakeProvider(), temp_store)
    good_manifest = run_selection([lead], "frozen", _selection([lead]), good_engine, temp_store, run_id="resume")
    assert good_manifest.completed == 1 and good_manifest.failed == 0
    cached = good_engine.analyze(lead, "resume")
    assert cached.cache_hit is True


def test_gate_6_unrecoverable_failure_stops_only_that_provider_run(lead_factory, temp_store) -> None:
    leads = [lead_factory(alias_suffix=letter * 10) for letter in ("F", "G", "H")]
    manifest = run_selection(
        leads,
        "frozen",
        _selection(leads),
        InspectorV2Engine(FakeProvider(no_citations=True), temp_store),
        temp_store,
        run_id="stop-provider",
    )
    assert manifest.completed == 1
    assert manifest.critical_failures == 1
    assert manifest.not_run == 2
    assert manifest.stopped_early is True
    assert manifest.stop_reason == "critical_failure"


def test_gate_7_reference_reconciliation_is_139_1_4() -> None:
    paths = [
        Path("/tmp/auditoria-conversas-2026-08.progress.json"),
        Path("/tmp/auditoria-conversas-2026-08.pending-codex.json"),
    ]
    corpus = Path("/tmp/acme-inspector-v2-corpus.json")
    assert corpus.exists() and all(path.exists() for path in paths)
    assert reconcile_reference(corpus, paths) == {
        "corpus": 143,
        "references": 140,
        "intersection": 139,
        "reference_outside_corpus": 1,
        "corpus_without_reference": 4,
    }


def test_gate_8_replay_has_zero_lookahead(lead_factory) -> None:
    lead = lead_factory(20)
    cutoff = datetime(2026, 8, 10, 23, 59, 59)
    truncated = truncate_for_cutoff(lead, cutoff)
    assert truncated is not None
    assert_no_lookahead([truncated], cutoff)
    assert all(
        message.timestamp is None or message.timestamp <= cutoff
        for message in parse_messages(truncated.mensagens)
    )
    assert len(replay_cutoffs([lead], 7)) == 7


def test_gate_8_replay_gate_rejects_incomplete_daily_run(temp_store) -> None:
    run_dirs = []
    for day in range(1, 8):
        run_dir = temp_store.root / f"day-{day}"
        run_dirs.append(run_dir)
        atomic_write_json(
            run_dir / "manifest.json",
            {
                "cutoff_at": f"2026-08-{day:02d}T23:59:59",
                "selected": 1,
                "completed": 0,
                "failed": 0,
                "skipped": 0,
                "not_run": 1,
                "stopped_early": True,
            },
        )
    assert replay_gate_ok(run_dirs) is False


def test_safety_package_has_no_productive_imports() -> None:
    root = Path("app/inspector_v2")
    forbidden = ("app.tools", "app.db", "app.integrations", "kommo", "whatsapp", "telegram", "delivery", "briefing")
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ] + [
            alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
        ]
        assert not any(any(blocked in imported.lower() for blocked in forbidden) for imported in imports), path


def test_supplied_redacted_sample_has_only_alias_identity() -> None:
    path = Path("/tmp/amostra-30-redigida.json")
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert len(rows) == 30
    forbidden = {"lead_id", "nome", "telefone", "corretora"}
    assert all(not (set(row) & forbidden) for row in rows)
