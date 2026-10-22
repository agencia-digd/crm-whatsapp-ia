"""Persistência privada, atômica e idempotente sob /tmp."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from app.inspector_v2.corpus import stable_hash
from app.inspector_v2.models import AnalysisResult, FailureRecord, RunManifest, SelectionManifest

ModelT = TypeVar("ModelT", bound=BaseModel)


def require_private_tmp(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    tmp = Path("/tmp").resolve()
    if resolved == tmp or not resolved.is_relative_to(tmp):
        raise ValueError(f"outputs com PII devem ficar em subdiretório de /tmp: {resolved}")
    return resolved


def atomic_write_json(path: Path, payload: dict[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_write_model(path: Path, model: BaseModel) -> None:
    atomic_write_json(path, model.model_dump(mode="json"))


def load_model(path: Path, model_type: type[ModelT]) -> ModelT:
    return model_type.model_validate_json(path.read_text(encoding="utf-8"))


class ShadowStore:
    def __init__(self, root: Path = Path("/tmp/acme-inspector-v2")) -> None:
        self.root = require_private_tmp(root)

    def selection_path(self, key: str) -> Path:
        return self.root / "selections" / f"{key}.json"

    def save_selection(self, manifest: SelectionManifest) -> Path:
        key = stable_hash(
            f"{manifest.corpus_fingerprint}\0{manifest.seed}\0{manifest.target_size}"
        )[:20]
        path = self.selection_path(key)
        if path.exists():
            existing = load_model(path, SelectionManifest)
            if existing != manifest:
                raise ValueError("colisão de chave de seleção")
            return path
        atomic_write_model(path, manifest)
        return path

    def run_dir(self, run_id: str) -> Path:
        return self.root / "runs" / run_id

    def result_path(self, run_id: str, lead_alias: str) -> Path:
        return self.run_dir(run_id) / "results" / f"{stable_hash(lead_alias)}.json"

    def failure_path(self, run_id: str, lead_alias: str) -> Path:
        return self.run_dir(run_id) / "failures" / f"{stable_hash(lead_alias)}.json"

    def load_cached_result(self, run_id: str, lead_alias: str, run_key: str) -> AnalysisResult | None:
        path = self.result_path(run_id, lead_alias)
        if not path.exists():
            return None
        result = load_model(path, AnalysisResult)
        return result if result.run_key == run_key else None

    def save_result(self, run_id: str, result: AnalysisResult) -> Path:
        path = self.result_path(run_id, result.lead_alias)
        if path.exists():
            existing = load_model(path, AnalysisResult)
            if existing.run_key != result.run_key:
                raise ValueError("resultado existente tem chave de execução diferente")
            return path
        atomic_write_model(path, result)
        self.failure_path(run_id, result.lead_alias).unlink(missing_ok=True)
        return path

    def save_failure(self, run_id: str, lead_alias: str, failure: FailureRecord) -> Path:
        path = self.failure_path(run_id, lead_alias)
        atomic_write_model(path, failure)
        return path

    def save_run_manifest(self, manifest: RunManifest) -> Path:
        path = self.run_dir(manifest.run_id) / "manifest.json"
        atomic_write_model(path, manifest)
        return path
