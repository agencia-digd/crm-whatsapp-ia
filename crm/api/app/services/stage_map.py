"""Helper de leitura do mapeamento de etapas (tabela `stage_map`).

Fonte única do mapeamento Kommo status_id ↔ Twenty stage ↔ label PT ↔ ordem.
Antes disso o mapa vivia hardcoded em 2 lugares (sync_kommo_twenty.py + config.ts
do dash), só com IDs — ver REVISAO-02-FUNIL gaps F4/F7, plano P1 item 4.

Cache em memória com TTL de 5min: o mapa muda raramente (renomeação de etapa,
nova coluna), então cada processo consulta o banco no máximo 1x a cada 5min.
Degrada gracioso: se o banco falha, devolve o último cache válido; se nunca houve
cache, devolve vazio (o caller decide o fallback — o sync cai no dict hardcoded).

Uso:
    from app.services import stage_map
    stage_map.stages_ordenados()          # [{"twenty_stage", "label_pt", ...}, ...]
    stage_map.label_de("VISITA_REAL")     # "Visita realizada" | None
    stage_map.stage_por_kommo_id("142")   # "CUSTOMER" | None
    stage_map.stages_validos()            # {"ENTRADA", "NEW", ...} (só ativos)
"""
from __future__ import annotations

import threading
import time
from typing import Any

import structlog
from sqlalchemy import text as sql_text

from app.db.session import SessionLocal

log = structlog.get_logger()

_TTL_SECONDS = 300  # 5 min
_lock = threading.Lock()
_cache: list[dict[str, Any]] | None = None
_cache_at: float = 0.0


def _now() -> float:
    return time.monotonic()


def _load_from_db() -> list[dict[str, Any]]:
    """Lê a tabela stage_map (só ativos) ordenada. Raise em erro (caller trata)."""
    with SessionLocal() as s:
        rows = s.execute(
            sql_text(
                "SELECT kommo_status_id, twenty_stage, label_pt, ordem, ativo "
                "FROM stage_map WHERE ativo = TRUE ORDER BY ordem, twenty_stage"
            )
        ).all()
    return [
        {
            "kommo_status_id": r[0],
            "twenty_stage": r[1],
            "label_pt": r[2],
            "ordem": int(r[3]) if r[3] is not None else 0,
            "ativo": bool(r[4]),
        }
        for r in rows
    ]


def _get(force: bool = False) -> list[dict[str, Any]]:
    """Devolve o mapa (cacheado). Recarrega se expirou ou force=True.

    Em erro de banco: mantém e devolve o último cache válido (ou vazio se nunca
    carregou). Nunca levanta — é caminho de leitura de UI/sync.
    """
    global _cache, _cache_at
    with _lock:
        fresh = _cache is not None and (_now() - _cache_at) < _TTL_SECONDS
        if fresh and not force:
            return _cache  # type: ignore[return-value]
        try:
            data = _load_from_db()
            _cache = data
            _cache_at = _now()
            return data
        except Exception:
            log.warning("stage_map_load_failed", exc_info=True)
            return _cache if _cache is not None else []


def refresh() -> None:
    """Força recarga do cache no próximo _get (invalida agora)."""
    global _cache_at
    with _lock:
        _cache_at = 0.0


def stages_ordenados() -> list[dict[str, Any]]:
    """Lista de stages ativos ordenada por `ordem` (pro kanban/config)."""
    return list(_get())


def stages_validos() -> set[str]:
    """Conjunto dos twenty_stage ativos — pra validar entrada de escrita."""
    return {row["twenty_stage"] for row in _get()}


def label_de(twenty_stage: str | None) -> str | None:
    """Label PT de um twenty_stage ativo, ou None se desconhecido."""
    if not twenty_stage:
        return None
    for row in _get():
        if row["twenty_stage"] == twenty_stage:
            return row["label_pt"]
    return None


def stage_por_kommo_id(kommo_status_id: str | int | None) -> str | None:
    """twenty_stage correspondente a um status_id do Kommo, ou None."""
    if kommo_status_id is None:
        return None
    key = str(kommo_status_id)
    for row in _get():
        if row["kommo_status_id"] is not None and str(row["kommo_status_id"]) == key:
            return row["twenty_stage"]
    return None


def mapa_kommo_para_stage() -> dict[str, str]:
    """{kommo_status_id: twenty_stage} dos stages ativos — conveniência pro sync."""
    return {
        str(row["kommo_status_id"]): row["twenty_stage"]
        for row in _get()
        if row["kommo_status_id"] is not None
    }
