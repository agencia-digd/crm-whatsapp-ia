"""
Cliente HTTP direto à API Kommo v4.

Padrões extraídos de /opt/acme-dash-api/update.py (em produção):
- Bearer auth com long-lived JWT token
- Paginação page/limit=250
- Retry em 429 (respeita Retry-After) e 5xx (exponencial)
- Timeout 30s por chamada
"""
import time

# ── Constantes do pipeline Acme ──────────────────────────────────────────
KOMMO_PIPELINE_ACME = 10000004
KOMMO_STAGE_ENTROU = 10000009
KOMMO_STAGE_EM_ATENDIMENTO = 10000005
KOMMO_STAGE_VISITA_AGENDADA = 10000002
KOMMO_STAGE_VISITA_REALIZADA = 10000012
KOMMO_STAGE_VENDA_GANHA = 142
KOMMO_STAGE_VENDA_PERDIDA = 143

KOMMO_STAGES_FUNIL = (
    KOMMO_STAGE_ENTROU,
    KOMMO_STAGE_EM_ATENDIMENTO,
    KOMMO_STAGE_VISITA_AGENDADA,
    KOMMO_STAGE_VISITA_REALIZADA,
    KOMMO_STAGE_VENDA_GANHA,
)
from collections.abc import Iterator
from typing import Any

import httpx
import structlog
from tenacity import RetryCallState, retry, retry_if_exception, stop_after_attempt
from tenacity.wait import wait_base

log = structlog.get_logger()


# ── Wait strategy: Retry-After pra 429, exponencial pra resto ─────────────

class _KommoWait(wait_base):
    def __call__(self, retry_state: RetryCallState) -> float:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
            retry_after = exc.response.headers.get("Retry-After", "2")
            try:
                return float(retry_after)
            except ValueError:
                return 2.0
        attempt = retry_state.attempt_number
        return min(float(2 ** attempt), 30.0)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(exc, (httpx.TimeoutException, httpx.ConnectError))


# ── Client ──────────────────────────────────────────────────────────────────

class KommoClient:
    """
    Cliente para a API Kommo v4. Thread-safe; cria um httpx.Client por chamada.

    Args:
        subdomain: subdomínio Kommo (ex: "agencia")
        token: long-lived Bearer JWT
        timeout: timeout em segundos (default 30)
    """

    def __init__(self, subdomain: str, token: str, timeout: int = 30) -> None:
        self.base_url = f"https://{subdomain}.kommo.com/api/v4"
        self.timeout = timeout
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=_KommoWait(),
        reraise=True,
    )
    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self.base_url + path
        log.debug("kommo_request", method="GET", path=path, params=params)
        with httpx.Client(headers=self._headers, timeout=self.timeout) as client:
            resp = client.get(url, params=params)
            log.debug("kommo_response", status=resp.status_code, path=path)
            if resp.status_code == 204:
                return {}
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 429:
                    log.warning("kommo_rate_limit", path=path,
                                retry_after=exc.response.headers.get("Retry-After"))
                elif status >= 500:
                    log.error("kommo_server_error", path=path, status=status)
                else:
                    log.error("kommo_client_error", path=path, status=status,
                              body=exc.response.text[:300])
                raise
            return resp.json()  # type: ignore[no-any-return]

    # ── Public methods ───────────────────────────────────────────────────────

    def get_account(self) -> dict[str, Any]:
        """Retorna dados da conta (sanity check de autenticação)."""
        return self._get("/account")

    def get_pipeline(self, pipeline_id: int) -> dict[str, Any]:
        """Retorna detalhes do pipeline incluindo estágios."""
        return self._get(f"/leads/pipelines/{pipeline_id}")

    def get_leads(
        self,
        filters: dict[str, Any],
        with_contacts: bool = True,
        limit: int = 250,
    ) -> Iterator[dict[str, Any]]:
        """
        Gerador paginado de leads.

        Inclui 'with=contacts' por padrão para extrair telefone.
        Segue exatamente o padrão do update.py em produção.
        """
        page = 1
        params: dict[str, Any] = {"limit": limit, "page": page, **filters}
        if with_contacts:
            params["with"] = "contacts"

        while True:
            params["page"] = page
            data = self._get("/leads", params=params)
            if not data:
                break
            items: list[dict[str, Any]] = data.get("_embedded", {}).get("leads", [])
            yield from items
            if len(items) < limit:
                break
            page += 1

    def get_lead(self, lead_id: int | str) -> dict[str, Any]:
        """Retorna lead completo com contatos embutidos."""
        return self._get(f"/leads/{lead_id}", params={"with": "contacts"})

    def get_users(self) -> list[dict[str, Any]]:
        """Lista usuários da conta (responsáveis = corretoras)."""
        data = self._get("/users")
        return data.get("_embedded", {}).get("users", [])  # type: ignore[return-value]

    def get_contacts_by_ids(self, contact_ids: list[int]) -> list[dict[str, Any]]:
        """
        Busca contatos por IDs com custom_fields (inclui telefone).

        A API Kommo não retorna custom_fields nos contatos embutidos em leads —
        é necessário este call separado para enriquecer telefones.

        Args:
            contact_ids: lista de IDs de contatos a buscar

        Returns:
            Lista de contatos com custom_fields_values preenchidos
        """
        if not contact_ids:
            return []
        params: dict[str, Any] = {
            "with": "custom_fields",
            "filter[id][]": contact_ids,
            "limit": 250,
        }
        data = self._get("/contacts", params=params)
        return data.get("_embedded", {}).get("contacts", [])  # type: ignore[return-value]

    def get_tasks(
        self,
        user_id: int,
        filter_overdue: bool = True,
        overdue_cutoff_ts: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Tarefas de um usuário específico.

        Args:
            user_id: Kommo user_id da corretora
            filter_overdue: se True, retorna apenas tarefas com prazo < cutoff
            overdue_cutoff_ts: Unix ts. Default = agora.
                Pra critério "atrasadas há ≥1 dia" passar início de hoje SP.
        """
        params: dict[str, Any] = {
            "filter[responsible_user_id]": user_id,
            "filter[is_completed]": 0,
        }
        if filter_overdue:
            cutoff = overdue_cutoff_ts if overdue_cutoff_ts is not None else int(time.time())
            params["filter[task_date][to]"] = cutoff

        data = self._get("/tasks", params=params)
        tasks: list[dict[str, Any]] = data.get("_embedded", {}).get("tasks", [])

        # A API Kommo ignora silenciosamente filter[is_completed] e filter[task_date][to].
        # Filtramos localmente (validado 06/05/2026).
        if filter_overdue:
            from datetime import datetime, timezone
            cutoff_ts = overdue_cutoff_ts if overdue_cutoff_ts is not None else int(datetime.now(timezone.utc).timestamp())
            tasks = [
                t for t in tasks
                if (t.get("complete_till") or 0) < cutoff_ts
                and not t.get("is_completed", False)
            ]
        else:
            tasks = [t for t in tasks if not t.get("is_completed", False)]

        return tasks  # type: ignore[return-value]

    # Status finais globais do Kommo (idênticos em todos pipelines)
    KOMMO_STATUS_WON = 142
    KOMMO_STATUS_LOST = 143

    def get_overdue_tasks_in_pipeline(
        self,
        user_id: int,
        pipeline_id: int,
        exclude_final_statuses: bool = True,
        overdue_cutoff_ts: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Tarefas atrasadas da corretora restritas a leads do pipeline e
        (opcionalmente) excluindo leads em status Won/Lost.

        Critério de "atrasada" (overdue_cutoff_ts):
          - None (default) = início do dia local SP. Conta apenas tarefas
            com prazo de OUTROS DIAS (não conta as vencendo hoje).
          - int = qualquer ts Unix.

        Implementação:
          1. Pega leads da corretora no pipeline (paginada).
          2. Filtra leads em Won/Lost se exclude_final_statuses.
          3. Pega tarefas com prazo < cutoff.
          4. Intersecta: tarefa.entity_type='leads' AND entity_id ∈ leads_ativos.
        """
        from datetime import datetime
        from zoneinfo import ZoneInfo

        if overdue_cutoff_ts is None:
            sp = ZoneInfo("America/Sao_Paulo")
            today_start_sp = datetime.now(sp).replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
            overdue_cutoff_ts = int(today_start_sp.timestamp())

        lead_filters: dict[str, Any] = {
            "filter[pipeline_id]": pipeline_id,
            "filter[responsible_user_id][]": user_id,
        }
        active_lead_ids: set[int] = set()
        for lead in self.get_leads(filters=lead_filters, with_contacts=False, limit=250):
            if exclude_final_statuses and lead.get("status_id") in (
                self.KOMMO_STATUS_WON, self.KOMMO_STATUS_LOST,
            ):
                continue
            active_lead_ids.add(int(lead["id"]))

        # Kommo `filter[task_date][to]` aplica em task_date, não em
        # complete_till. Pra usar cutoff de "atrasada de fato", buscamos
        # cru (sem filtro temporal) e aplicamos em Python sobre complete_till.
        tasks = self.get_tasks(user_id=user_id, filter_overdue=False)
        return [
            t for t in tasks
            if t.get("entity_type") == "leads"
            and t.get("entity_id") is not None
            and int(t["entity_id"]) in active_lead_ids
            and (t.get("complete_till") or 0) < overdue_cutoff_ts
        ]

    def get_status_changed_events(
        self,
        pipeline_id: int,
        from_unix: int,
        to_unix: int,
        page_size: int = 250,
    ) -> list[dict[str, Any]]:
        """
        GET /api/v4/events paginado, filtrado por data e pipeline.

        A API não tem filtro nativo de pipeline — retorna events de toda
        a conta. Filtramos em Python: event conta se pipeline_id aparece
        em value_before OU value_after.

        Returns lista de dicts:
          {lead_id, created_by, created_at, stage_from, stage_to, pipeline_id}

        stage_from/stage_to = value_before/after.lead_status.id.
        Se um dos lados não tem lead_status (ex: lead novo), usa 0.
        """
        params: dict[str, Any] = {
            "filter[type][]": "lead_status_changed",
            "filter[entity]": "leads",
            "filter[created_at][from]": from_unix,
            "filter[created_at][to]": to_unix,
            "limit": page_size,
            "page": 1,
        }

        result: list[dict[str, Any]] = []
        while True:
            data = self._get("/events", params=params)
            raw_events: list[dict[str, Any]] = (
                data.get("_embedded", {}).get("events", [])
            )

            for e in raw_events:
                vb = (e.get("value_before") or [{}])[0].get("lead_status") or {}
                va = (e.get("value_after") or [{}])[0].get("lead_status") or {}
                pipe_before = vb.get("pipeline_id", 0)
                pipe_after = va.get("pipeline_id", 0)

                if pipe_before != pipeline_id and pipe_after != pipeline_id:
                    continue

                result.append({
                    "lead_id": int(e.get("entity_id") or 0),
                    "created_by": int(e.get("created_by") or 0),
                    "created_at": int(e.get("created_at") or 0),
                    "stage_from": int(vb.get("id") or 0),
                    "stage_to": int(va.get("id") or 0),
                    "pipeline_id": pipeline_id,
                })

            if not data.get("_links", {}).get("next"):
                break
            params["page"] = params["page"] + 1  # type: ignore[operator]

        log.info(
            "kommo_status_events",
            pipeline_id=pipeline_id,
            from_unix=from_unix,
            to_unix=to_unix,
            total=len(result),
        )
        return result

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=_KommoWait(),
        reraise=True,
    )
    def _patch(self, path: str, payload: dict[str, Any] | list[Any]) -> dict[str, Any]:
        url = self.base_url + path
        log.debug("kommo_request", method="PATCH", path=path)
        with httpx.Client(headers=self._headers, timeout=self.timeout) as client:
            resp = client.patch(url, json=payload)
            log.debug("kommo_response", status=resp.status_code, path=path)
            resp.raise_for_status()
            return resp.json() if resp.content else {}  # type: ignore[no-any-return]

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=_KommoWait(),
        reraise=True,
    )
    def _post(self, path: str, payload: dict[str, Any] | list[Any]) -> dict[str, Any]:
        url = self.base_url + path
        log.debug("kommo_request", method="POST", path=path)
        with httpx.Client(headers=self._headers, timeout=self.timeout) as client:
            resp = client.post(url, json=payload)
            log.debug("kommo_response", status=resp.status_code, path=path)
            resp.raise_for_status()
            return resp.json() if resp.content else {}  # type: ignore[no-any-return]

    def update_lead_stage(
        self, lead_id: int | str, status_id: int, pipeline_id: int | None = None
    ) -> dict[str, Any]:
        """Move lead pra outra etapa do funil.

        Args:
            lead_id: ID do lead no Kommo.
            status_id: ID da etapa de destino.
            pipeline_id: opcional, pra mudança entre pipelines.
        """
        payload: dict[str, Any] = {"status_id": int(status_id)}
        if pipeline_id is not None:
            payload["pipeline_id"] = int(pipeline_id)
        return self._patch(f"/leads/{lead_id}", payload)

    def add_lead_note(
        self, lead_id: int | str, text: str, note_type: str = "common"
    ) -> dict[str, Any]:
        """Adiciona nota textual a um lead.

        Args:
            lead_id: ID do lead.
            text: corpo da nota.
            note_type: 'common' (default) é nota livre visível na timeline.
        """
        payload = [{
            "note_type": note_type,
            "params": {"text": text},
        }]
        return self._post(f"/leads/{lead_id}/notes", payload)

    def get_leads_by_ids(
        self,
        lead_ids: list[int],
        chunk_size: int = 50,
        pipeline_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Busca leads por lista de IDs em batches.

        Usado para obter lead.price dos leads que entraram em Venda Ganha.
        chunk_size=50 evita URLs longas demais.
        pipeline_id: se fornecido, filtra pelo pipeline (lado servidor quando possível,
                     sempre validado localmente).
        """
        result: list[dict[str, Any]] = []
        for i in range(0, len(lead_ids), chunk_size):
            chunk = lead_ids[i : i + chunk_size]
            params: dict[str, Any] = {"filter[id][]": chunk, "limit": chunk_size}
            if pipeline_id is not None:
                params["filter[pipeline_id]"] = pipeline_id
            data = self._get("/leads", params=params)
            result.extend(data.get("_embedded", {}).get("leads", []))
        return result
