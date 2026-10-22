"""Rubrica determinística; referências mistas são rótulo inicial, não verdade perfeita."""

from __future__ import annotations

import json
import math
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.inspector_v2.models import AnalysisPayload, AnalysisResult
from app.inspector_v2.preparation import stable_alias


class EvaluationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    selected: int = Field(ge=0)
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)
    evaluated_against_reference: int = Field(ge=0)
    reference_note: str
    gates: dict[str, bool]
    gate_failures: list[str]
    critical_failures: int = Field(ge=0)
    d1_joint_intention_temperature: float | None = Field(default=None, ge=0, le=1)
    intention_accuracy: float | None = Field(default=None, ge=0, le=1)
    temperature_accuracy: float | None = Field(default=None, ge=0, le=1)
    d2_visit_accuracy: float | None = Field(default=None, ge=0, le=1)
    d3_gap_progress_accuracy: float | None = Field(default=None, ge=0, le=1)
    d3_gap_pending_accuracy: float | None = Field(default=None, ge=0, le=1)
    d4_next_action_adequacy: float | None = Field(default=None, ge=0, le=1)
    d5_grounding: float | None = Field(default=None, ge=0, le=1)
    d6_completeness: float | None = Field(default=None, ge=0, le=1)
    d7_confidence_calibration: float | None = Field(default=None, ge=0, le=1)
    d8_temporal_fidelity: float | None = Field(default=None, ge=0, le=1)
    d9_latency_cost: float | None = Field(default=None, ge=0, le=1)
    diagnostic_multivalue_f1: float | None = Field(default=None, ge=0, le=1)
    diagnostic_narrative_similarity: float | None = Field(default=None, ge=0, le=1)
    weighted_score: float | None = Field(default=None, ge=0, le=100)
    latency_p50_ms: int | None = Field(default=None, ge=0)
    latency_p95_ms: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    failure_types: dict[str, int]
    eligible_for_ranking: bool


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text.lower())).strip()


def _tokens(value: Any) -> set[str]:
    return {token for token in _normalize(value).split() if len(token) > 2}


def _similarity(left: Any, right: Any) -> float:
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    if not left_tokens and not right_tokens:
        return 1.0
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _accuracy(pairs: list[tuple[str, str]]) -> float | None:
    return sum(left == right for left, right in pairs) / len(pairs) if pairs else None


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _grounding_score(results: list[AnalysisResult]) -> float:
    """Average grounding per lead; a lead without citations contributes zero."""
    if not results:
        return 0.0
    return statistics.fmean(
        result.checks.citacoes_validas / result.checks.citacoes_total
        if result.checks.citacoes_total
        else 0.0
        for result in results
    )


def _match_counts(expected: list[str], predicted: list[str], threshold: float = 0.35) -> tuple[int, int, int]:
    available = set(range(len(predicted)))
    matches = 0
    for expected_item in expected:
        candidates = [(index, _similarity(expected_item, predicted[index])) for index in available]
        if not candidates:
            continue
        best_index, score = max(candidates, key=lambda item: item[1])
        if score >= threshold:
            matches += 1
            available.remove(best_index)
    return matches, len(predicted), len(expected)


def _load_references(paths: list[Path]) -> dict[str, dict[str, Any]]:
    references: dict[str, dict[str, Any]] = {}
    for path in paths:
        decoded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(decoded, list):
            raise ValueError(f"referência deve ser lista: {path}")
        source = "pending" if "pending" in path.name else "progress"
        for item in decoded:
            if not isinstance(item, dict) or not isinstance(item.get("lead_id"), str):
                continue
            alias = stable_alias("LEAD", item["lead_id"])
            if alias in references:
                raise ValueError("referência duplicada")
            references[alias] = {**item, "_source": source, "lead_id": None, "lead_nome": None}
    return references


def reconcile_reference(raw_corpus_path: Path, reference_paths: list[Path]) -> dict[str, int]:
    corpus = json.loads(raw_corpus_path.read_text(encoding="utf-8"))
    corpus_ids = {str(item["lead_id"]) for item in corpus if isinstance(item, dict) and item.get("lead_id") is not None}
    reference_ids: set[str] = set()
    for path in reference_paths:
        decoded = json.loads(path.read_text(encoding="utf-8"))
        reference_ids.update(
            str(item["lead_id"])
            for item in decoded
            if isinstance(item, dict) and item.get("lead_id") is not None
        )
    return {
        "corpus": len(corpus_ids),
        "references": len(reference_ids),
        "intersection": len(corpus_ids & reference_ids),
        "reference_outside_corpus": len(reference_ids - corpus_ids),
        "corpus_without_reference": len(corpus_ids - reference_ids),
    }


def load_results(run_dir: Path) -> list[AnalysisResult]:
    result_dir = run_dir / "results"
    if not result_dir.exists():
        return []
    return [AnalysisResult.model_validate_json(path.read_text(encoding="utf-8")) for path in sorted(result_dir.glob("*.json"))]


def _action_family(action: str) -> str:
    normalized = _normalize(action)
    if any(token in normalized for token in ("opt out", "parar mensagens", "encerrar", "nao contatar")):
        return "encerrar"
    if "aguard" in normalized or "esper" in normalized:
        return "aguardar"
    if any(token in normalized for token in ("reativ", "retom", "recontat")):
        return "reativar"
    if any(token in normalized for token in ("visita", "agendar", "horario")):
        return "propor_visita"
    if any(token in normalized for token in ("renda", "entrada", "fgts", "financ", "simul")):
        return "coletar_financeiro"
    return "outra"


def _action_is_adequate(payload: AnalysisPayload) -> bool:
    family = _action_family(payload.proxima_acao)
    temperature = payload.temperatura_atual.value
    intention = payload.intencao_maxima.value
    if temperature == "quente":
        return family in {"propor_visita", "coletar_financeiro", "outra"}
    if temperature == "frio" and intention == "alta":
        return family in {"reativar", "aguardar", "coletar_financeiro"}
    if temperature == "frio" and intention in {"baixa", "sem_evidencia"}:
        return family in {"aguardar", "encerrar", "reativar"}
    if temperature == "sem_evidencia":
        return family != "propor_visita"
    return family in {"reativar", "propor_visita", "coletar_financeiro", "outra"}


def _completeness(payload: AnalysisPayload) -> float:
    evidence_sections = [
        payload.evidencias_intencao,
        payload.financeiro,
        payload.objecoes,
        payload.perguntas_sem_resposta,
    ]
    checks = [
        bool(payload.resumo_jornada.strip()),
        bool(payload.proxima_acao.strip()),
        payload.citacoes is not None,
        payload.gap_corretora is None or bool(payload.citacoes),
        all(all(item.citacao.strip() and item.mensagem_id >= 1 for item in section) for section in evidence_sections),
        len(payload.citacoes) >= 3,
    ]
    return sum(checks) / len(checks)


def _candidate_values(payload: AnalysisPayload, field: str) -> list[str]:
    collection = getattr(payload, field)
    return [item.leitura for item in collection]


def _reference_values(reference: dict[str, Any], field: str) -> list[str]:
    value = reference.get(field)
    return [str(item) for item in value] if isinstance(value, list) else []


def _sum_optional(values: list[int | None]) -> int | None:
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def evaluate_replay(run_dirs: list[Path]) -> float | None:
    by_alias: dict[str, list[AnalysisResult]] = defaultdict(list)
    for run_dir in run_dirs:
        for result in load_results(run_dir):
            by_alias[result.lead_alias].append(result)
    comparisons = valid = 0
    order = {"sem_evidencia": 0, "baixa": 1, "media": 2, "alta": 3}
    for results in by_alias.values():
        ordered = sorted(results, key=lambda result: result.cutoff_at or datetime.min)
        for previous, current in zip(ordered, ordered[1:]):
            comparisons += 1
            no_lookahead = current.checks.lookahead_messages == 0
            non_decreasing = order[current.payload.intencao_maxima.value] >= order[previous.payload.intencao_maxima.value]
            valid += no_lookahead and non_decreasing
    return valid / comparisons if comparisons else None


def replay_gate_ok(run_dirs: list[Path]) -> bool:
    if len(run_dirs) != 7:
        return False
    cutoffs: list[datetime] = []
    for run_dir in run_dirs:
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            return False
        decoded = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(decoded, dict):
            return False
        cutoff = decoded.get("cutoff_at")
        if not isinstance(cutoff, str):
            return False
        cutoffs.append(datetime.fromisoformat(cutoff.replace("Z", "+00:00")))
        selected = int(decoded.get("selected", -1))
        if (
            int(decoded.get("completed", -1)) != selected
            or int(decoded.get("failed", 0)) != 0
            or int(decoded.get("skipped", 0)) != 0
            or int(decoded.get("not_run", 0)) != 0
            or bool(decoded.get("stopped_early", False))
        ):
            return False
        results = load_results(run_dir)
        if len(results) != selected or any(result.checks.lookahead_messages for result in results):
            return False
    ordered = sorted(value.date() for value in cutoffs)
    return len(set(ordered)) == 7 and all(
        (current - previous).days == 1 for previous, current in zip(ordered, ordered[1:])
    )


def evaluate_run(
    run_dir: Path,
    reference_paths: list[Path],
    *,
    selected: int | None = None,
    replay_run_dirs: list[Path] | None = None,
) -> EvaluationSummary:
    results = load_results(run_dir)
    references = _load_references(reference_paths)
    failure_paths = sorted((run_dir / "failures").glob("*.json")) if (run_dir / "failures").exists() else []
    manifest: dict[str, Any] = {}
    if (run_dir / "manifest.json").exists():
        decoded = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        if isinstance(decoded, dict):
            manifest = decoded
    selected_count = selected if selected is not None else int(manifest.get("selected", len(results) + len(failure_paths)))
    provider = results[0].provider if results else str(manifest.get("provider", "unknown"))
    model = results[0].model if results else str(manifest.get("model", "unknown"))
    paired = [(result, references[result.lead_alias]) for result in results if result.lead_alias in references]

    intention_pairs = [(_normalize(ref.get("intencao_maxima")), result.payload.intencao_maxima.value) for result, ref in paired]
    temperature_pairs = [(_normalize(ref.get("temperatura_atual")), result.payload.temperatura_atual.value) for result, ref in paired]
    visit_pairs = [(_normalize(ref.get("visita")), result.payload.visita.value) for result, ref in paired]
    joint = [left_i == right_i and left_t == right_t for (left_i, right_i), (left_t, right_t) in zip(intention_pairs, temperature_pairs)]

    gap_by_source: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for result, ref in paired:
        expected = "presente" if ref.get("gap_corretora") is not None else "ausente"
        predicted = "presente" if result.payload.gap_corretora is not None else "ausente"
        gap_by_source[str(ref["_source"])].append((expected, predicted))

    matched = predicted_total = expected_total = 0
    narrative: list[float] = []
    for result, ref in paired:
        for field in ("financeiro", "objecoes", "perguntas_sem_resposta", "evidencias_intencao"):
            m, p, e = _match_counts(_reference_values(ref, field), _candidate_values(result.payload, field))
            matched += m
            predicted_total += p
            expected_total += e
        narrative.extend(
            [
                _similarity(ref.get("resumo_jornada"), result.payload.resumo_jornada),
                _similarity(ref.get("gap_corretora"), result.payload.gap_corretora),
                _similarity(ref.get("proxima_acao"), result.payload.proxima_acao),
            ]
        )
    precision = matched / predicted_total if predicted_total else (1.0 if expected_total == 0 else 0.0)
    recall = matched / expected_total if expected_total else 1.0
    multivalue_f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    grounding = _grounding_score(results)
    min_citations_ok = all(result.checks.citacoes_total >= 3 for result in results)
    coverage_ok = bool(results) and all(
        result.cobertura == 1.0 and result.mensagens_processadas == result.mensagens_entrada
        for result in results
    )
    lookahead_ok = all(result.checks.lookahead_messages == 0 for result in results)
    critical_failures = sum(result.checks.falha_critica for result in results)
    failures_ok = len(failure_paths) == 0 if selected_count <= 30 else len(failure_paths) / selected_count <= 0.02
    replay_ok = replay_gate_ok(replay_run_dirs) if replay_run_dirs else False
    d8 = evaluate_replay(replay_run_dirs) if replay_ok and replay_run_dirs else None
    gates = {
        "full_frozen_sample_30": selected_count == 30 and len(results) + len(failure_paths) == 30,
        "coverage_100pct": coverage_ok,
        "zero_unauthorized_skips": int(manifest.get("skipped", 0)) == 0,
        "failure_rate_lte_2pct": failures_ok,
        "grounding_gte_98pct_and_min_3": grounding >= 0.98 and min_citations_ok,
        "zero_lookahead": lookahead_ok,
        "zero_critical_failures": critical_failures == 0,
        "replay_7d_evaluated": replay_ok and d8 is not None,
    }
    gate_failures = [name for name, passed in gates.items() if not passed]

    d1 = sum(joint) / len(joint) if joint else None
    d4 = statistics.fmean(_action_is_adequate(result.payload) for result in results) if results else None
    d6 = statistics.fmean(_completeness(result.payload) for result in results) if results else None
    confidence_values = {"baixa": 0.3, "media": 0.6, "alta": 0.9}
    calibration_items = [
        1 - abs(confidence_values[result.payload.confianca.value] - float(correct))
        for (result, _), correct in zip(paired, joint)
    ]
    d7 = statistics.fmean(calibration_items) if calibration_items else None
    latencies = [result.latency_ms for result in results]
    d9 = None if not results else 1.0 / (1.0 + ((_percentile(latencies, 0.95) or 0) / 60_000))

    dimensions = {
        "d1": (d1, 20),
        "d2": (_accuracy(visit_pairs), 10),
        "d3": (
            statistics.fmean(
                value for value in (
                    _accuracy(gap_by_source.get("progress", [])),
                    _accuracy(gap_by_source.get("pending", [])),
                ) if value is not None
            ) if gap_by_source else None,
            10,
        ),
        "d4": (d4, 15),
        "d5": (grounding, 15),
        "d6": (d6, 10),
        "d7": (d7, 5),
        "d8": (d8, 10),
        "d9": (d9, 5),
    }
    known_weight = sum(weight for value, weight in dimensions.values() if value is not None)
    weighted_score = (
        sum((value or 0.0) * weight for value, weight in dimensions.values() if value is not None) * 100 / known_weight
        if known_weight else None
    )

    failure_types: Counter[str] = Counter()
    for path in failure_paths:
        decoded = json.loads(path.read_text(encoding="utf-8"))
        failure_types[str(decoded.get("error_type", "unknown"))] += 1
    costs = [result.usage.cost_usd for result in results if result.usage.cost_usd is not None]
    return EvaluationSummary(
        provider=provider,
        model=model,
        selected=selected_count,
        completed=len(results),
        failed=len(failure_paths),
        evaluated_against_reference=len(paired),
        reference_note=(
            "Auditoria inicial rasa e mista; progress e pending têm vieses de anotação distintos. "
            "Enums são comparados como rótulo inicial, gap é reportado separadamente e texto tem peso zero."
        ),
        gates=gates,
        gate_failures=gate_failures,
        critical_failures=critical_failures,
        d1_joint_intention_temperature=d1,
        intention_accuracy=_accuracy(intention_pairs),
        temperature_accuracy=_accuracy(temperature_pairs),
        d2_visit_accuracy=_accuracy(visit_pairs),
        d3_gap_progress_accuracy=_accuracy(gap_by_source.get("progress", [])),
        d3_gap_pending_accuracy=_accuracy(gap_by_source.get("pending", [])),
        d4_next_action_adequacy=d4,
        d5_grounding=grounding if results else None,
        d6_completeness=d6,
        d7_confidence_calibration=d7,
        d8_temporal_fidelity=d8,
        d9_latency_cost=d9,
        diagnostic_multivalue_f1=multivalue_f1 if paired else None,
        diagnostic_narrative_similarity=statistics.fmean(narrative) if narrative else None,
        weighted_score=weighted_score,
        latency_p50_ms=_percentile(latencies, 0.50),
        latency_p95_ms=_percentile(latencies, 0.95),
        input_tokens=_sum_optional([result.usage.input_tokens for result in results]),
        output_tokens=_sum_optional([result.usage.output_tokens for result in results]),
        total_tokens=_sum_optional([result.usage.total_tokens for result in results]),
        cost_usd=sum(costs) if costs else None,
        failure_types=dict(failure_types),
        eligible_for_ranking=not gate_failures,
    )


def choose_provider(summaries: list[EvaluationSummary]) -> EvaluationSummary | None:
    eligible = [summary for summary in summaries if summary.eligible_for_ranking]
    if len(eligible) < 2:
        return None
    return max(
        eligible,
        key=lambda summary: (
            summary.weighted_score or 0.0,
            summary.d1_joint_intention_temperature or 0.0,
            summary.d5_grounding or 0.0,
            summary.d4_next_action_adequacy or 0.0,
            summary.d8_temporal_fidelity or 0.0,
            -(summary.latency_p95_ms or 10**12),
        ),
    )
