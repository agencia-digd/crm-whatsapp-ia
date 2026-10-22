"""Relatório agregado sem PII, escolha condicionada e cutover seguro."""

from __future__ import annotations

from datetime import date

from app.inspector_v2.evaluation import EvaluationSummary, choose_provider


def _pct(value: float | None) -> str:
    return "n/d" if value is None else f"{value * 100:.1f}%"


def render_report(summaries: list[EvaluationSummary], preflight: dict[str, dict[str, object]] | None = None) -> str:
    eligible = [summary for summary in summaries if summary.eligible_for_ranking]
    winner = choose_provider(summaries)
    lines = [
        "# Inspector V2 Acme — relatório agregado de decisão",
        "",
        f"**Gerado em:** {date.today().isoformat()}  ",
        "**Escopo:** piloto em sombra, corpus redigido, zero escrita operacional",
        "",
        "## Decisão",
        "",
    ]
    if winner is not None:
        lines.append(f"**Recomendação para canário futuro:** {winner.provider} / {winner.model}.")
    elif len(eligible) == 1:
        only = eligible[0]
        lines.append(
            f"**Sem vencedor comparável.** {only.provider} / {only.model} foi o único sobrevivente dos gates; isso não prova superioridade."
        )
    else:
        lines.append("**Nenhuma decisão de modelo autorizada.** São necessários pelo menos dois providers RUN_OK no mesmo recorte.")

    lines.extend(
        [
            "",
            "> A auditoria de referência é rasa e mistura lotes/anotadores. Enums são rótulos iniciais; gap é medido por lote; textos têm peso zero no ranking.",
        ]
    )
    if preflight:
        lines.extend(["", "## Preflight", ""])
        for provider in sorted(preflight):
            available = "RUNNABLE" if preflight[provider].get("available") else "NOT_RUN"
            model = preflight[provider].get("model") or "padrão"
            lines.append(f"- {provider}: {available}; modelo `{model}`")

    lines.extend(["", "## Rubrica agregada", ""])
    if not summaries:
        lines.append("Nenhuma bateria comparável concluída; métricas permanecem `n/d`.")
    for summary in summaries:
        lines.extend(
            [
                f"### {summary.provider} / {summary.model}",
                "",
                f"- Execução: {summary.completed}/{summary.selected} completed; {summary.failed} failed",
                f"- Gates: {'PASS' if summary.eligible_for_ranking else 'FAIL'} — {', '.join(summary.gate_failures) or 'nenhum blocker'}",
                f"- Score conhecido: {f'{summary.weighted_score:.1f}/100' if summary.weighted_score is not None else 'n/d'}",
                f"- D1 intenção×temperatura: {_pct(summary.d1_joint_intention_temperature)}",
                f"- D2 visita: {_pct(summary.d2_visit_accuracy)}",
                f"- D3 gap progress/pending: {_pct(summary.d3_gap_progress_accuracy)} / {_pct(summary.d3_gap_pending_accuracy)}",
                f"- D4 próxima ação: {_pct(summary.d4_next_action_adequacy)}",
                f"- D5 grounding: {_pct(summary.d5_grounding)}",
                f"- D6 completude: {_pct(summary.d6_completeness)}",
                f"- D7 confiança: {_pct(summary.d7_confidence_calibration)}",
                f"- D8 fidelidade temporal: {_pct(summary.d8_temporal_fidelity)}",
                f"- Latência p50/p95: {summary.latency_p50_ms or 'n/d'} / {summary.latency_p95_ms or 'n/d'} ms",
                f"- Custo marginal conhecido: {f'USD {summary.cost_usd:.4f}' if summary.cost_usd is not None else 'n/d'}",
                "",
            ]
        )

    lines.extend(
        [
            "## Runbook de cutover futuro",
            "",
            "1. `INSPECTOR_V2_ENABLED=false` por default e `INSPECTOR_V2_SHADOW=true`.",
            "2. Exigir 2+ providers RUN_OK, 0/30 falhas, grounding ≥98%, ≥3 citações/caso, cobertura 100% e look-ahead zero.",
            "3. Resolver paridade de ingestão: sem corte 200 e sem teto Cloud 720h.",
            "4. Canário de uma corretora por ≥14 dias; V1 segue gravando e alimentando briefings.",
            "5. Revisão cega semanal ≥30 e zero erro grave antes de ampliar para as três corretoras.",
            "6. Anotações e próximas ações do V2 nunca são executadas automaticamente.",
            "",
            "## Rollback",
            "",
            "- Alterar uma única flag para `INSPECTOR_V2_ENABLED=false`.",
            "- Manter V1 e `lead_analyses` intactos durante todo o canário.",
            "- Preservar artefatos sombra em `/tmp` para auditoria; nenhuma migration destrutiva é necessária.",
            "",
            "## Observado versus pendente",
            "",
            "- Observado nesta entrega: replay histórico local, testes, corpus redigido e resultados realmente executados listados acima.",
            "- Pendente de dias corridos: shadow live de 3–7 dias; cutover exige canário de 14 dias e não é autorizado por este piloto.",
            "",
        ]
    )
    return "\n".join(lines)
