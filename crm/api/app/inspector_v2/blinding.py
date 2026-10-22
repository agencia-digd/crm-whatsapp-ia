"""Pacote cego dos maiores desacordos, sempre privado em /tmp."""

from __future__ import annotations

from pathlib import Path

from app.inspector_v2.corpus import load_redacted_corpus, stable_hash
from app.inspector_v2.evaluation import load_results
from app.inspector_v2.models import AnalysisResult
from app.inspector_v2.storage import atomic_write_json, require_private_tmp


def build_blind_review(
    run_dirs: list[Path],
    redacted_corpus_path: Path,
    output_dir: Path,
    *,
    limit: int = 10,
    seed: str = "acme-painel-blind-2026-08",
) -> tuple[Path, Path]:
    if len(run_dirs) < 2:
        raise ValueError("revisão cega exige pelo menos dois providers")
    by_provider: dict[str, dict[str, AnalysisResult]] = {}
    for run_dir in run_dirs:
        loaded = load_results(run_dir)
        if loaded:
            by_provider[loaded[0].provider] = {result.lead_alias: result for result in loaded}
    if len(by_provider) < 2:
        raise ValueError("menos de dois providers têm resultados")
    providers = sorted(by_provider)
    labels = {
        provider: f"anon_{index + 1}"
        for index, provider in enumerate(sorted(providers, key=lambda value: stable_hash(f"{seed}\0{value}")))
    }
    common_aliases = set.intersection(*(set(results) for results in by_provider.values()))
    corpus, _ = load_redacted_corpus(redacted_corpus_path)
    messages_by_alias = {lead.lead_alias: lead.mensagens for lead in corpus}

    def disagreement(alias: str) -> tuple[int, str]:
        projections = {
            (
                result.payload.intencao_maxima.value,
                result.payload.temperatura_atual.value,
                result.payload.visita.value,
                result.payload.gap_corretora is not None,
            )
            for provider_results in by_provider.values()
            if (result := provider_results.get(alias)) is not None
        }
        return len(projections), stable_hash(f"{seed}\0{alias}")

    selected = sorted(common_aliases, key=disagreement, reverse=True)[:limit]
    packet = []
    for alias in selected:
        candidates = {}
        for provider, provider_results in by_provider.items():
            result = provider_results[alias]
            candidates[labels[provider]] = {
                "payload": result.payload.model_dump(mode="json"),
                "checks": result.checks.model_dump(mode="json"),
            }
        packet.append(
            {
                "lead_alias": alias,
                "mensagens_redigidas": messages_by_alias.get(alias, []),
                "candidates": candidates,
            }
        )
    root = require_private_tmp(output_dir)
    packet_path = root / "blind-top10.json"
    map_path = root / "blind-provider-map.json"
    atomic_write_json(packet_path, packet)
    atomic_write_json(map_path, {"seed": seed, "providers": labels})
    return packet_path, map_path
