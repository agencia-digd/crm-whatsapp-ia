"""
GatewayClient — wrapper REST mínimo pro gateway Gateway AI.

Gateway encapsula OAuth do Google Calendar (e futuramente Sheets) por trás de
um Bearer token gerenciado. Usado pela Assistente pra ler/escrever no calendário
`contato@example.com`.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from app.config import settings

log = structlog.get_logger()

SP_TZ = ZoneInfo("America/Sao_Paulo")


class GatewayClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        calendar_id: str | None = None,
    ) -> None:
        self.api_key = api_key or settings.gateway_api_key
        self.base_url = (base_url or settings.gateway_base_url).rstrip("/")
        self.calendar_id = calendar_id or settings.gateway_calendar_id
        if not self.api_key:
            raise RuntimeError("GATEWAY_API_KEY não configurado (.env)")

    # ── HTTP ──────────────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 204:
                return {}
            return json.loads(resp.read())

    def _cal_path(self, suffix: str = "") -> str:
        return f"/calendars/{urllib.parse.quote(self.calendar_id)}/events{suffix}"

    # ── Calendar ──────────────────────────────────────────────────────────

    def list_events(
        self,
        date_range: str | None = None,
        time_min: datetime | None = None,
        time_max: datetime | None = None,
        max_results: int = 250,
    ) -> list[dict[str, Any]]:
        """
        Lista eventos do calendário Acme.

        date_range:
            'today'      → 00:00 a 23:59 SP de hoje
            'this_week'  → segunda 00:00 a domingo 23:59 SP
            None         → usa time_min/time_max explícitos (ou últimos 7d)
        """
        if date_range == "today":
            now = datetime.now(SP_TZ)
            time_min = now.replace(hour=0, minute=0, second=0, microsecond=0)
            time_max = time_min + timedelta(days=1)
        elif date_range == "this_week":
            now = datetime.now(SP_TZ)
            monday = now - timedelta(days=now.weekday())
            time_min = monday.replace(hour=0, minute=0, second=0, microsecond=0)
            time_max = time_min + timedelta(days=7)
        elif time_min is None or time_max is None:
            time_max = datetime.now(SP_TZ)
            time_min = time_max - timedelta(days=7)

        params = {
            "timeMin": time_min.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "timeMax": time_max.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": max_results,
        }
        resp = self._request("GET", self._cal_path(), params=params)
        return resp.get("items", [])

    def create_event(
        self,
        summary: str,
        start_iso: str,
        end_iso: str,
        color_id: str | None = None,
        description: str | None = None,
        time_zone: str = "America/Sao_Paulo",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "summary": summary,
            "start": {"dateTime": start_iso, "timeZone": time_zone},
            "end": {"dateTime": end_iso, "timeZone": time_zone},
        }
        if color_id:
            body["colorId"] = color_id
        if description:
            body["description"] = description
        return self._request("POST", self._cal_path(), body=body)

    def update_event(self, event_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "PATCH",
            self._cal_path(f"/{urllib.parse.quote(event_id)}"),
            body=patch,
        )

    def get_event(self, event_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            self._cal_path(f"/{urllib.parse.quote(event_id)}"),
        )
