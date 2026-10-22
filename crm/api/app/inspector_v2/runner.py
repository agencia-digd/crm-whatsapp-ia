"""Runs retomáveis e replay sem look-ahead sobre aliases redigidos."""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone

import structlog

from app.inspector_v2.corpus import manifest_fingerprint, parse_messages, stable_hash
from app.inspector_v2.engine import InspectorV2Engine
from app.inspector_v2.models import FailureRecord, RedactedLead, RunManifest, SelectionManifest
from app.inspector_v2.storage import ShadowStore

log = structlog.get_logger()


def run_selection(
    leads: list[RedactedLead],
    corpus_fingerprint: str,
    selection: SelectionManifest,
    engine: InspectorV2Engine,
    store: ShadowStore,
    *,
    run_id: str | None = None,
    cutoff_at: datetime | None = None,
    force: bool = False,
) -> RunManifest:
    by_alias = {lead.lead_alias: lead for lead in leads}
    selection_hash = manifest_fingerprint(selection)
    effective_run_id = run_id or stable_hash(
        f"{corpus_fingerprint}\0{selection_hash}\0{engine.provider.name}\0{engine.provider.model}\0"
        f"{json.dumps(engine.provider.runtime_configuration(), sort_keys=True, separators=(',', ':'))}\0"
        f"{cutoff_at.isoformat() if cutoff_at else ''}"
    )[:20]
    manifest = RunManifest(
        run_id=effective_run_id,
        corpus_fingerprint=corpus_fingerprint,
        provider=engine.provider.name,
        model=engine.provider.model,
        runtime=engine.provider.runtime_configuration(),
        selection_fingerprint=selection_hash,
        cutoff_at=cutoff_at,
        started_at=datetime.now(timezone.utc),
        selected=len(selection.items),
    )
    store.save_run_manifest(manifest)
    for position, item in enumerate(selection.items):
        lead = by_alias.get(item.lead_alias)
        if lead is None:
            # No replay, isso significa "ainda não existia", não skip de conteúdo.
            continue
        try:
            result = engine.analyze(lead, effective_run_id, cutoff_at=cutoff_at, force=force)
            manifest.completed += 1
            if result.cache_hit:
                manifest.cache_hits += 1
            if result.checks.falha_critica:
                manifest.critical_failures += 1
                manifest.stopped_early = True
                manifest.stop_reason = "critical_failure"
                manifest.not_run = len(selection.items) - position - 1
                store.save_run_manifest(manifest)
                break
        except Exception as exc:  # falha de modelo/schema nunca vira skip
            manifest.failed += 1
            failure = FailureRecord(
                run_id=effective_run_id,
                run_key="unavailable",
                provider=engine.provider.name,
                model=engine.provider.model,
                runtime=engine.provider.runtime_configuration(),
                lead_alias=lead.lead_alias,
                cutoff_at=cutoff_at,
                error_type=type(exc).__name__,
                error_message=str(exc)[:500],
                parse_attempts=min(max(int(getattr(exc, "attempts", 1)), 1), 2),
                failed_at=datetime.now(timezone.utc),
            )
            store.save_failure(effective_run_id, lead.lead_alias, failure)
            log.warning(
                "inspector_v2_shadow_failed",
                lead_alias=lead.lead_alias,
                provider=engine.provider.name,
                error_type=failure.error_type,
            )
            manifest.stopped_early = True
            manifest.stop_reason = "provider_failure"
            manifest.not_run = len(selection.items) - position - 1
        store.save_run_manifest(manifest)
        if manifest.stopped_early:
            break
    manifest.completed_at = datetime.now(timezone.utc)
    store.save_run_manifest(manifest)
    return manifest


def replay_cutoffs(leads: list[RedactedLead], days: int = 7) -> list[datetime]:
    timestamps = [
        message.timestamp
        for lead in leads
        for message in parse_messages(lead.mensagens)
        if message.timestamp is not None
    ]
    if not timestamps:
        raise ValueError("corpus não possui timestamps parseáveis")
    final_date = max(timestamps).date()
    return [
        datetime.combine(final_date - timedelta(days=offset), time(23, 59, 59))
        for offset in reversed(range(days))
    ]


def truncate_for_cutoff(lead: RedactedLead, cutoff_at: datetime) -> RedactedLead | None:
    parsed = parse_messages(lead.mensagens)
    messages = [
        message.raw
        for message in parsed
        if message.timestamp is None or message.timestamp <= cutoff_at
    ]
    if not messages:
        return None
    return lead.model_copy(update={"mensagens": messages, "mensagens_total": len(messages)})


def assert_no_lookahead(leads: list[RedactedLead], cutoff_at: datetime) -> None:
    future = [
        message
        for lead in leads
        for message in parse_messages(lead.mensagens)
        if message.timestamp is not None and message.timestamp > cutoff_at
    ]
    if future:
        raise ValueError(f"replay contém {len(future)} mensagens posteriores ao corte")


def replay_selection(
    leads: list[RedactedLead],
    corpus_fingerprint: str,
    selection: SelectionManifest,
    engine: InspectorV2Engine,
    store: ShadowStore,
    *,
    days: int = 7,
    force: bool = False,
) -> list[RunManifest]:
    selected_aliases = {item.lead_alias for item in selection.items}
    manifests: list[RunManifest] = []
    for cutoff_at in replay_cutoffs(leads, days):
        truncated = [
            candidate
            for lead in leads
            if lead.lead_alias in selected_aliases
            if (candidate := truncate_for_cutoff(lead, cutoff_at)) is not None
        ]
        assert_no_lookahead(truncated, cutoff_at)
        daily_aliases = {lead.lead_alias for lead in truncated}
        daily_selection = selection.model_copy(
            update={"items": [item for item in selection.items if item.lead_alias in daily_aliases]}
        )
        daily_fingerprint = stable_hash(
            "\0".join(f"{lead.lead_alias}:{len(lead.mensagens)}" for lead in truncated)
        )
        run_id = stable_hash(
            f"replay\0{corpus_fingerprint}\0{cutoff_at.isoformat()}\0"
            f"{engine.provider.name}\0{engine.provider.model}\0{manifest_fingerprint(selection)}"
            f"\0{json.dumps(engine.provider.runtime_configuration(), sort_keys=True, separators=(',', ':'))}"
        )[:20]
        manifests.append(
            run_selection(
                truncated,
                daily_fingerprint,
                daily_selection,
                engine,
                store,
                run_id=run_id,
                cutoff_at=cutoff_at,
                force=force,
            )
        )
        if manifests[-1].stopped_early:
            break
    return manifests
