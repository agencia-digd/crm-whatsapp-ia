"""CLI reproduzível do piloto sombra; todos os brutos permanecem em /tmp."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.inspector_v2.corpus import load_redacted_corpus, load_selection_manifest
from app.inspector_v2.blinding import build_blind_review
from app.inspector_v2.engine import InspectorV2Engine
from app.inspector_v2.evaluation import EvaluationSummary, evaluate_run
from app.inspector_v2.providers import CodexCliProvider, GrokCliProvider, QwenTokenPlanProvider, ShadowProvider
from app.inspector_v2.preparation import assert_safe
from app.inspector_v2.reporting import render_report
from app.inspector_v2.runner import replay_selection, run_selection
from app.inspector_v2.storage import ShadowStore, atomic_write_json, require_private_tmp


def _provider(name: str, args: argparse.Namespace) -> ShadowProvider:
    if name == "codex":
        return CodexCliProvider(model=args.codex_model, timeout_seconds=args.timeout)
    if name == "grok":
        return GrokCliProvider(model=args.grok_model, timeout_seconds=args.timeout)
    if name == "qwen":
        return QwenTokenPlanProvider(
            model="qwen3.8-max",
            ssh_host=args.qwen_host,
            remote_command="/usr/local/bin/qwen-token-plan",
            timeout_seconds=args.timeout,
        )
    raise ValueError(f"provider desconhecido: {name}")


def _add_provider_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--codex-model", default="default")
    parser.add_argument("--grok-model", default="default")
    parser.add_argument("--qwen-host", default="vps")
    parser.add_argument("--timeout", type=int, default=300)


def _load_inputs(args: argparse.Namespace) -> tuple[list[Any], str, Any]:
    leads, fingerprint = load_redacted_corpus(args.corpus)
    assert_safe(leads)
    selection = load_selection_manifest(args.manifest, fingerprint)
    limit = getattr(args, "limit", None)
    if limit is not None:
        if limit <= 0:
            raise ValueError("--limit deve ser positivo")
        selection = selection.model_copy(
            update={"items": selection.items[:limit], "target_size": min(limit, len(selection.items))}
        )
    aliases = getattr(args, "lead_alias", None)
    if aliases:
        requested = set(aliases)
        filtered = [item for item in selection.items if item.lead_alias in requested]
        found = {item.lead_alias for item in filtered}
        if missing_requested := requested - found:
            raise ValueError(f"--lead-alias possui {len(missing_requested)} alias ausente do manifesto")
        selection = selection.model_copy(update={"items": filtered, "target_size": len(filtered)})
    selected_aliases = {item.lead_alias for item in selection.items}
    available = {lead.lead_alias for lead in leads}
    missing = selected_aliases - available
    if missing:
        raise ValueError(f"manifesto possui {len(missing)} aliases ausentes do corpus redigido")
    return leads, fingerprint, selection


def _preflight(args: argparse.Namespace) -> dict[str, dict[str, object]]:
    return {
        name: dict(_provider(name, args).preflight())
        for name in ("codex", "grok", "qwen")
    }


def _engine(provider: ShadowProvider, store: ShadowStore, args: argparse.Namespace) -> InspectorV2Engine:
    return InspectorV2Engine(
        provider,
        store,
        max_chunk_characters=args.chunk_chars,
        max_reduce_characters=args.reduce_chars,
        overlap_messages=args.overlap,
        retries=1,
    )


def command_preflight(args: argparse.Namespace) -> int:
    payload = _preflight(args)
    output = require_private_tmp(args.output)
    atomic_write_json(output, payload)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return 0


def command_run(args: argparse.Namespace) -> int:
    leads, fingerprint, selection = _load_inputs(args)
    store = ShadowStore(args.output_root)
    provider = _provider(args.provider, args)
    preflight = provider.preflight()
    if not preflight.get("available"):
        raise RuntimeError(f"provider NOT_RUN: {args.provider}")
    manifest = run_selection(
        leads,
        fingerprint,
        selection,
        _engine(provider, store, args),
        store,
        run_id=args.run_id,
        force=args.force,
    )
    sys.stdout.write(json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False) + "\n")
    return 0


def command_benchmark(args: argparse.Namespace) -> int:
    leads, fingerprint, selection = _load_inputs(args)
    store = ShadowStore(args.output_root)
    manifests: list[dict[str, Any]] = []
    for provider_name in ("codex", "grok", "qwen"):
        provider = _provider(provider_name, args)
        preflight = provider.preflight()
        if not preflight.get("available"):
            manifests.append({"provider": provider_name, "status": "NOT_RUN", "preflight": preflight})
            continue
        manifest = run_selection(
            leads,
            fingerprint,
            selection,
            _engine(provider, store, args),
            store,
            force=args.force,
        )
        manifests.append({"provider": provider_name, "status": "RUN_OK", "manifest": manifest.model_dump(mode="json")})
    output = store.root / "benchmark-manifests.json"
    atomic_write_json(output, manifests)
    sys.stdout.write(json.dumps(manifests, ensure_ascii=False) + "\n")
    return 0


def command_replay(args: argparse.Namespace) -> int:
    leads, fingerprint, selection = _load_inputs(args)
    store = ShadowStore(args.output_root)
    provider = _provider(args.provider, args)
    if not provider.preflight().get("available"):
        raise RuntimeError(f"provider NOT_RUN: {args.provider}")
    manifests = replay_selection(
        leads,
        fingerprint,
        selection,
        _engine(provider, store, args),
        store,
        days=7,
        force=args.force,
    )
    payload = [manifest.model_dump(mode="json") for manifest in manifests]
    atomic_write_json(store.root / f"replay-{provider.name}-manifests.json", payload)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return 0


def command_evaluate(args: argparse.Namespace) -> int:
    summary = evaluate_run(
        args.run_dir,
        args.references,
        replay_run_dirs=args.replay_run_dirs or None,
    )
    output = require_private_tmp(args.output)
    atomic_write_json(output, summary.model_dump(mode="json"))
    sys.stdout.write(summary.model_dump_json() + "\n")
    return 0


def command_report(args: argparse.Namespace) -> int:
    summaries = [EvaluationSummary.model_validate_json(path.read_text(encoding="utf-8")) for path in args.evaluations]
    preflight: dict[str, dict[str, object]] | None = None
    if args.preflight and args.preflight.exists():
        decoded = json.loads(args.preflight.read_text(encoding="utf-8"))
        if isinstance(decoded, dict):
            preflight = decoded
    report = render_report(summaries, preflight)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    sys.stdout.write(str(args.output) + "\n")
    return 0


def command_blind(args: argparse.Namespace) -> int:
    packet, mapping = build_blind_review(args.run_dirs, args.corpus, args.output_dir, limit=10)
    sys.stdout.write(json.dumps({"packet": str(packet), "mapping": str(mapping)}, ensure_ascii=False) + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspector V2 Acme em modo sombra")
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser("preflight")
    _add_provider_options(preflight)
    preflight.add_argument("--output", type=Path, default=Path("/tmp/acme-inspector-v2/preflight.json"))
    preflight.set_defaults(handler=command_preflight)

    def add_run_inputs(target: argparse.ArgumentParser) -> None:
        target.add_argument("--corpus", type=Path, default=Path("/tmp/amostra-30-redigida.json"))
        target.add_argument("--manifest", type=Path, default=Path("/tmp/amostra-30-manifesto.json"))
        target.add_argument("--output-root", type=Path, default=Path("/tmp/acme-inspector-v2"))
        target.add_argument("--chunk-chars", type=int, default=32_000)
        target.add_argument("--reduce-chars", type=int, default=48_000)
        target.add_argument("--overlap", type=int, default=2)
        target.add_argument("--force", action="store_true")
        target.add_argument("--limit", type=int)
        target.add_argument("--lead-alias", action="append")
        _add_provider_options(target)

    run = subparsers.add_parser("run")
    add_run_inputs(run)
    run.add_argument("--provider", choices=("codex", "grok", "qwen"), required=True)
    run.add_argument("--run-id")
    run.set_defaults(handler=command_run)

    benchmark = subparsers.add_parser("benchmark")
    add_run_inputs(benchmark)
    benchmark.set_defaults(handler=command_benchmark)

    replay = subparsers.add_parser("replay")
    add_run_inputs(replay)
    replay.add_argument("--provider", choices=("codex", "grok", "qwen"), required=True)
    replay.set_defaults(handler=command_replay)

    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("--run-dir", type=Path, required=True)
    evaluate.add_argument("--references", type=Path, nargs="+", required=True)
    evaluate.add_argument("--replay-run-dirs", type=Path, nargs="*")
    evaluate.add_argument("--output", type=Path, required=True)
    evaluate.set_defaults(handler=command_evaluate)

    report = subparsers.add_parser("report")
    report.add_argument("--evaluations", type=Path, nargs="*", default=[])
    report.add_argument("--preflight", type=Path)
    report.add_argument("--output", type=Path, required=True)
    report.set_defaults(handler=command_report)

    blind = subparsers.add_parser("blind")
    blind.add_argument("--run-dirs", type=Path, nargs="+", required=True)
    blind.add_argument("--corpus", type=Path, default=Path("/tmp/amostra-30-redigida.json"))
    blind.add_argument("--output-dir", type=Path, default=Path("/tmp/acme-inspector-v2/blind-review"))
    blind.set_defaults(handler=command_blind)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.handler(args))
