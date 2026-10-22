"""Cliente REST do Twenty (workspace Acme) — leitura de opps + escrita de etapa.

Twenty roda local em :3333, REST em /rest/opportunities (+GraphQL). Este cliente
cobre o que a Missão 11 precisa: achar a opportunity de um lead (via kommoLeadId),
ler uma opp por id, e MUDAR a etapa (stage) de uma opp — a base do "mudar etapa"
da Assistente (REVISAO-06-ASSISTENTE §3, prioridade 1 estrutural).

Credencial (LAZY, tolerante a ausência — padrão de app/services/cloud_lines.py):
  /etc/agencia/twenty-acme.env → TWENTY_API_URL, TWENTY_API_KEY. Em teste/worktree os
  arquivos de /etc/agencia podem não existir, e isso NÃO é erro — a leitura só falha na
  hora de usar (chamada de rede), nunca na importação. NUNCA loga o valor do token.

Escrita (update_stage) tem 3 travas contra escrita acidental:
  1. guard PYTEST_CURRENT_TEST — se rodando sob pytest, loga e NÃO chama a rede.
  2. settings.dry_run — se True, loga o PATCH e devolve resultado simulado.
  3. rate limit / retry — 2 tentativas extras em 429/5xx, backoff simples.

Shape do PATCH confirmado em scripts/sync_kommo_twenty.py (produção):
  PATCH /rest/opportunities/{id}  body={"stage": "<VALUE>"}  (Bearer, JSON).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import structlog

from app.config import settings

log = structlog.get_logger()

_ENV_FILE = os.environ.get("TWENTY_ENV", "/etc/agencia/twenty-acme.env")
_TIMEOUT = 15.0
_MAX_RETRIES = 2  # tentativas EXTRA em 429/5xx (total até 3 chamadas)


class TwentyConfigError(RuntimeError):
    """Credencial do Twenty ausente/incompleta no momento do uso."""


class TwentyClient:
    """Cliente REST do Twenty. Lê credencial LAZY do env-file (não na init)."""

    def __init__(self, env_file: str | None = None) -> None:
        self._env_file = env_file or _ENV_FILE
        self._url: str | None = None
        self._key: str | None = None
        self._loaded = False

    # ── credencial (lazy, tolerante) ────────────────────────────────────────
    def _parse_env_file(self, path: str) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            raw = Path(path).read_text()
        except OSError:
            return out
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            out[key.strip()] = val.strip().strip('"').strip("'")
        return out

    def _ensure_creds(self) -> None:
        if self._loaded:
            return
        env = self._parse_env_file(self._env_file)
        self._url = (env.get("TWENTY_API_URL") or "").rstrip("/")
        self._key = env.get("TWENTY_API_KEY") or ""
        self._loaded = True

    @property
    def configured(self) -> bool:
        """True se URL e KEY estão presentes (sem levantar). NUNCA loga o token."""
        self._ensure_creds()
        return bool(self._url and self._key)

    def _headers(self) -> dict[str, str]:
        self._ensure_creds()
        if not self._url or not self._key:
            raise TwentyConfigError(
                "Twenty sem credencial: configure TWENTY_API_URL e TWENTY_API_KEY "
                f"em {self._env_file}"
            )
        return {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}

    # ── transporte com retry (429/5xx) ──────────────────────────────────────
    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = self._headers()
        url = f"{self._url}{path}"
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                with httpx.Client(timeout=_TIMEOUT) as c:
                    r = c.request(method, url, headers=headers, json=body)
                if r.status_code in (429, 500, 502, 503, 504) and attempt < _MAX_RETRIES:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                r.raise_for_status()
                if not r.content:
                    return {}
                return r.json()
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code in (429, 500, 502, 503, 504) and attempt < _MAX_RETRIES:
                    last_exc = e
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_exc = e
                if attempt < _MAX_RETRIES:
                    time.sleep(1.0)
                    continue
                raise
        if last_exc:
            raise last_exc
        raise RuntimeError("twenty_request_sem_resposta")  # pragma: no cover

    # ── leitura ─────────────────────────────────────────────────────────────
    def get_opportunity(self, opportunity_id: str) -> dict[str, Any] | None:
        """Retorna a opp por id (ou None se 404)."""
        try:
            d = self._request("GET", f"/rest/opportunities/{opportunity_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        return _unwrap_opportunity(d)

    def get_opportunity_by_kommo_id(self, kommo_lead_id: str | int) -> dict[str, Any] | None:
        """Acha a opp cujo kommoLeadId == lead_id do Kommo (ou None).

        Usa o filtro REST do Twenty. Se o filtro não vier suportado, cai num
        retorno vazio (None) — o caller trata como "fora do piloto".
        """
        kid = str(kommo_lead_id)
        # filtro REST do Twenty: ?filter=field[eq]:value
        path = f"/rest/opportunities?filter=kommoLeadId[eq]:{kid}&limit=1"
        d = self._request("GET", path)
        opps = d.get("data", {}).get("opportunities", []) if isinstance(d, dict) else []
        for o in opps:
            if str(o.get("kommoLeadId")) == kid:
                return o
        return None

    # ── escrita (mudar etapa) ───────────────────────────────────────────────
    def update_stage(self, opportunity_id: str, novo_stage: str) -> dict[str, Any]:
        """PATCH da etapa (stage) de uma opp.

        Travas de escrita (nesta ordem):
          1. rodando sob pytest (PYTEST_CURRENT_TEST) → NÃO escreve; loga e devolve
             {"dry_run": True, "reason": "pytest", ...}.
          2. settings.dry_run True → NÃO escreve; loga e devolve simulado.
          3. senão → PATCH real com retry.

        Shape do PATCH: /rest/opportunities/{id} body={"stage": novo_stage}
        (idêntico ao scripts/sync_kommo_twenty.py).
        """
        payload = {"stage": novo_stage}

        if os.environ.get("PYTEST_CURRENT_TEST"):
            log.info("twenty_update_stage_skip_pytest", opp=opportunity_id, stage=novo_stage)
            return {"dry_run": True, "reason": "pytest", "id": opportunity_id, "stage": novo_stage}

        if settings.dry_run:
            log.info("twenty_update_stage_dry_run", opp=opportunity_id, stage=novo_stage)
            return {"dry_run": True, "reason": "settings.dry_run", "id": opportunity_id, "stage": novo_stage}

        d = self._request("PATCH", f"/rest/opportunities/{opportunity_id}", payload)
        log.info("twenty_update_stage_ok", opp=opportunity_id, stage=novo_stage)
        return _unwrap_opportunity(d) or {"id": opportunity_id, "stage": novo_stage}

    # ── escrita (criar nota no lead) ─────────────────────────────────────────
    def add_note_to_lead(self, kommo_lead_id: str | int, texto: str) -> bool:
        """Cria uma Note no Twenty e a liga à Opportunity do lead (via kommoLeadId).

        Fluxo REST (schema confirmado no OpenAPI /open-api/core do workspace):
          1. resolve opportunityId via get_opportunity_by_kommo_id(kommo_lead_id);
             se o lead não tem opp no Twenty → loga e retorna False (auditoria
             local do caller fica, mas não houve post externo).
          2. POST /rest/notes  body={"title": ..., "bodyV2": {"markdown": texto}}
             → retorna a note criada (com id).
          3. POST /rest/noteTargets  body={"noteId": <id>, "targetOpportunityId": <opp>}
             → liga a note à opp.

        Travas de escrita (mesma disciplina de update_stage):
          1. rodando sob pytest → NÃO escreve; loga e retorna True (simulado ok).
          2. settings.dry_run True → NÃO escreve; loga e retorna True.

        Retorna True se criou+ligou; False em qualquer erro/ausência de opp
        (loga e não explode — o caller trata como "não postou externamente").
        """
        opp = self.get_opportunity_by_kommo_id(kommo_lead_id)
        opp_id = (opp or {}).get("id") if isinstance(opp, dict) else None
        if not opp_id:
            log.info("twenty_add_note_sem_opp", kommo_lead_id=str(kommo_lead_id))
            return False

        if os.environ.get("PYTEST_CURRENT_TEST"):
            log.info("twenty_add_note_skip_pytest", opp=opp_id, kommo_lead_id=str(kommo_lead_id))
            return True

        if settings.dry_run:
            log.info("twenty_add_note_dry_run", opp=opp_id, kommo_lead_id=str(kommo_lead_id))
            return True

        try:
            note_body = {
                "title": "Observação (Copiloto)",
                "bodyV2": {"markdown": texto},
            }
            created = self._request("POST", "/rest/notes", note_body)
            note_id = _extract_id(created, "note", "createNote")
            if not note_id:
                log.warning("twenty_add_note_sem_id", opp=opp_id, resposta_keys=list(created.keys()) if isinstance(created, dict) else None)
                return False

            self._request(
                "POST",
                "/rest/noteTargets",
                {"noteId": note_id, "targetOpportunityId": opp_id},
            )
            log.info("twenty_add_note_ok", opp=opp_id, note=note_id, kommo_lead_id=str(kommo_lead_id))
            return True
        except Exception:
            log.exception("twenty_add_note_falhou", opp=opp_id, kommo_lead_id=str(kommo_lead_id))
            return False


def _extract_id(d: dict[str, Any] | None, *envelope_keys: str) -> str | None:
    """Extrai o id de uma resposta de criação do Twenty (envelope variável).

    Twenty responde ora {"data": {"createNote": {...}}}, ora
    {"data": {"note": {...}}}, ora o dict cru com "id" no topo.
    """
    if not isinstance(d, dict):
        return None
    data = d.get("data")
    if isinstance(data, dict):
        for k in envelope_keys:
            inner = data.get(k)
            if isinstance(inner, dict) and inner.get("id"):
                return str(inner["id"])
        if data.get("id"):
            return str(data["id"])
    if d.get("id"):
        return str(d["id"])
    return None


def _unwrap_opportunity(d: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normaliza o envelope de resposta do Twenty pra dict da opp.

    O Twenty responde ora {"data": {"opportunity": {...}}} (single),
    ora {"data": {"updateOpportunity": {...}}}, ora o dict cru. Cobre os 3.
    """
    if not isinstance(d, dict):
        return None
    data = d.get("data")
    if isinstance(data, dict):
        for k in ("opportunity", "updateOpportunity", "createOpportunity"):
            if isinstance(data.get(k), dict):
                return data[k]
        # às vezes data já é a opp
        if "id" in data:
            return data
    if "id" in d:
        return d
    return None
