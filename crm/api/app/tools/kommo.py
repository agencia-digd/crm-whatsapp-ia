"""
KommoTool — Agno Toolkit para dados do CRM Kommo (pipeline Acme).

⚠️  REGRA CRÍTICA DE SEGURANÇA ⚠️
Toda query nesta tool aplica filtro implícito:
    pipeline_id = settings.kommo_pipeline_id_main (10000004)

Isso impede que o agente acesse leads de outros pipelines da conta
agencia (ex: Escala Imóveis, outros clientes). NÃO remover esse filtro.
"""
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from agno.tools import Toolkit

from app.config import settings
from app.integrations.kommo_client import KommoClient

log = structlog.get_logger()

SP_TZ = ZoneInfo("America/Sao_Paulo")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _today_sp_ts() -> int:
    """Timestamp Unix do início do dia de hoje em São Paulo (00:00:00)."""
    now_sp = datetime.now(SP_TZ)
    start_sp = now_sp.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start_sp.timestamp())


def _normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) in (10, 11) and not digits.startswith("55"):
        digits = "55" + digits
    return "+" + digits if digits else ""


def _extract_phone(lead: dict[str, Any]) -> str:
    contacts = lead.get("_embedded", {}).get("contacts", [])
    for contact in contacts:
        for field in contact.get("custom_fields_values") or []:
            if field.get("field_code") == "PHONE":
                values = field.get("values") or []
                if values:
                    return _normalize_phone(values[0].get("value", ""))
    return ""


def _flatten_lead(lead: dict[str, Any]) -> dict[str, Any]:
    """Converte resposta aninhada da API em dict plano."""
    return {
        "lead_id": str(lead.get("id", "")),
        "nome": lead.get("name", ""),
        "telefone": _extract_phone(lead),
        "kommo_user_id": lead.get("responsible_user_id"),
        "etapa_id": lead.get("status_id"),
        "pipeline_id": lead.get("pipeline_id"),
        "origem": _extract_origem(lead),
        "criado_em": lead.get("created_at"),
        "atualizado_em": lead.get("updated_at"),
    }


def _phone_from_contact(contact: dict[str, Any]) -> str:
    """Extrai telefone de um contato com custom_fields_values preenchido."""
    for field in contact.get("custom_fields_values") or []:
        if field.get("field_code") == "PHONE":
            values = field.get("values") or []
            if values:
                return _normalize_phone(values[0].get("value", ""))
    return ""


def _extract_origem(lead: dict[str, Any]) -> str:
    for field in lead.get("custom_fields_values") or []:
        code = field.get("field_code", "")
        if code in ("UTM_SOURCE", "LEAD_SOURCE", "ORIGEM"):
            values = field.get("values") or []
            if values:
                return str(values[0].get("value", ""))
    return ""


# ── KommoTool ────────────────────────────────────────────────────────────────

class KommoTool(Toolkit):
    """
    Ferramenta de acesso ao CRM Kommo para o pipeline Acme Imóveis.

    ⚠️  PIPELINE LOCK: pipeline_id=10000004 é aplicado em toda query.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            name="kommo_tool",
            tools=[
                self.get_leads_today,
                self.get_leads_with_activity_today,
                self.get_lead_details,
                self.get_overdue_tasks,
                self.get_pipeline_stages,
                self.move_lead_stage,
                self.add_lead_note,
            ],
            **kwargs,
        )
        self._client = KommoClient(
            subdomain=settings.kommo_subdomain,
            token=settings.kommo_token,
        )
        self._pipeline_id = settings.kommo_pipeline_id_main

    def get_leads_today(self) -> str:
        """
        Lista leads criados hoje no pipeline da Acme (pipeline_id implícito).

        Returns:
            JSON com lista de leads criados a partir de 00:00 (horário SP).
            Cada item: {lead_id, nome, telefone, kommo_user_id, etapa_id, origem, criado_em}
        """
        import json
        ts = _today_sp_ts()
        filters = {
            "filter[pipeline_id]": self._pipeline_id,
            "filter[created_at][from]": ts,
        }
        leads = list(self._client.get_leads(filters))
        flat = [_flatten_lead(l) for l in leads]
        log.info("kommo_leads_today", count=len(flat), since_ts=ts)
        return json.dumps(flat, ensure_ascii=False, default=str)

    def get_leads_with_activity_today(self) -> str:
        """
        Lista leads com atividade hoje no pipeline da Acme.

        Filtra por updated_at >= 00:00 SP. Inclui leads novos E leads
        que receberam mensagem, mudaram de etapa, etc.

        Returns:
            JSON com lista de leads (mesmo schema de get_leads_today).
        """
        import json
        ts = _today_sp_ts()
        filters = {
            "filter[pipeline_id]": self._pipeline_id,
            "filter[updated_at][from]": ts,
        }
        leads = list(self._client.get_leads(filters))
        flat = [_flatten_lead(l) for l in leads]
        log.info("kommo_leads_activity", count=len(flat), since_ts=ts)
        return json.dumps(flat, ensure_ascii=False, default=str)

    def get_lead_details(self, lead_id: str) -> str:
        """
        Retorna dados completos de um lead específico.

        Args:
            lead_id: ID do lead no Kommo.

        Returns:
            JSON com campos do lead incluindo telefone e contatos.
        """
        import json
        lead = self._client.get_lead(lead_id)
        flat = _flatten_lead(lead)
        log.info("kommo_lead_details", lead_id=lead_id)
        return json.dumps(flat, ensure_ascii=False, default=str)

    def get_overdue_tasks(self, corretora_kommo_user_id: int) -> str:
        """
        Lista tarefas atrasadas de UMA corretora específica.

        ⚠️  PRIVACIDADE: retorna APENAS tarefas da corretora informada.
        Nunca chame com user_id de outra corretora.

        Args:
            corretora_kommo_user_id: Kommo user_id da corretora
                (Pessoa6=10000006, Pessoa1=10000007, Pessoa3=10000003)

        Returns:
            JSON com lista de tarefas:
            {task_id, lead_id, entity_id, entity_type, tipo, text,
             due_at, complete_till_at, dias_atrasada}
        """
        import json
        import time as _time
        tasks = self._client.get_tasks(corretora_kommo_user_id, filter_overdue=True)
        now_ts = _time.time()

        # Filtrar por pipeline: descartar tarefas vinculadas a leads de outros pipelines
        lead_ids_in_tasks = [
            t["entity_id"] for t in tasks
            if t.get("entity_type") == "leads" and t.get("entity_id")
        ]
        if lead_ids_in_tasks:
            leads_in_pipeline = self._client.get_leads_by_ids(
                lead_ids_in_tasks, pipeline_id=self._pipeline_id
            )
            valid_lead_ids = {str(lead["id"]) for lead in leads_in_pipeline}
            discarded = [
                t for t in tasks
                if t.get("entity_type") == "leads"
                and str(t.get("entity_id", "")) not in valid_lead_ids
            ]
            if discarded:
                log.warning(
                    "kommo_tasks_cross_pipeline_discarded",
                    total_discarded=len(discarded),
                    user_id=corretora_kommo_user_id,
                )
            tasks = [
                t for t in tasks
                if t.get("entity_type") != "leads"
                or str(t.get("entity_id", "")) in valid_lead_ids
            ]

        normalized = []
        for t in tasks:
            complete_till = t.get("complete_till") or 0
            dias = max(0, int((now_ts - complete_till) / 86400)) if complete_till else 0
            entity_type = t.get("entity_type", "")
            entity_id = t.get("entity_id")
            lead_id = str(entity_id) if entity_type == "leads" and entity_id else None
            normalized.append({
                "task_id": t.get("id"),
                "lead_id": lead_id,
                "entity_id": entity_id,
                "entity_type": entity_type,
                "tipo": t.get("text") or "follow-up",
                "text": t.get("text", ""),
                "due_at": complete_till,
                "complete_till_at": complete_till,
                "dias_atrasada": dias,
            })
        log.info("kommo_overdue_tasks", user_id=corretora_kommo_user_id, count=len(normalized))
        return json.dumps(normalized, ensure_ascii=False, default=str)

    def get_overdue_tasks_for_lead(
        self,
        lead_id: str,
        corretora_kommo_user_id: int,
    ) -> list[dict[str, Any]]:
        """
        Retorna tarefas atrasadas de UM lead específico.

        ⚠️  PRIVACIDADE: sempre filtra por corretora_kommo_user_id antes de
        filtrar por lead_id — garante isolamento entre corretoras.

        Para processamento em batch (Inspector), prefira get_overdue_tasks()
        uma vez e filtrar com _get_lead_tasks_detail() no pipeline.

        Args:
            lead_id: ID do lead no Kommo (como string)
            corretora_kommo_user_id: Kommo user_id da corretora

        Returns:
            Lista de dicts com tarefas atrasadas deste lead.
        """
        import json
        all_tasks: list[dict[str, Any]] = json.loads(
            self.get_overdue_tasks(corretora_kommo_user_id)
        )
        return [
            t for t in all_tasks
            if str(t.get("entity_id", "")) == str(lead_id)
        ]

    def get_leads_for_inspector(
        self,
        kommo_user_id: int,
        days_back: int = 2,
    ) -> list[dict[str, Any]]:
        """
        Lista leads de uma corretora para o Lead Inspector.

        Retorna a union de:
        - Leads criados nos últimos N dias (created_at >= now - days_back)
        - Leads atualizados hoje (updated_at >= 00:00 SP)

        Deduplica por lead_id. Não exposto como Agno Tool (uso programático pelo Inspector).

        Args:
            kommo_user_id: responsible_user_id no Kommo da corretora
            days_back: janela de criação em dias (default 2)

        Returns:
            list[dict] com campos achatados — mesmo schema de get_leads_today
        """
        import json
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        SP_TZ = ZoneInfo("America/Sao_Paulo")
        now_sp = datetime.now(SP_TZ)

        # Janela de updated_at = últimas 72h. Cobre:
        #  - Leads atualizados à noite entre run-evening (19:30 SP) e meia-noite
        #  - Fim de semana (lead atualizado sex à noite ainda aparece na run de dom)
        # Caso de referência (2026-05-17): Pessoa8, lead antigo (created_at junho/25)
        # movido pra Entrou às 21:28 SP de sexta, escapava da janela "00:00 hoje"
        # E também da janela "24h" quando rodada no domingo à noite.
        ts_updated_from = int((now_sp - timedelta(hours=72)).timestamp())
        ts_created_from = int(
            (now_sp - timedelta(days=days_back))
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )

        base_filters: dict[str, Any] = {
            "filter[pipeline_id]": self._pipeline_id,
            "filter[responsible_user_id][]": kommo_user_id,
            "with": "contacts",
        }

        raw_leads_created = list(self._client.get_leads({**base_filters, "filter[created_at][from]": ts_created_from}))
        raw_leads_updated = list(self._client.get_leads({**base_filters, "filter[updated_at][from]": ts_updated_from}))

        # Deduplicar por lead_id
        seen: set[str] = set()
        raw_leads: list[dict[str, Any]] = []
        for lead in raw_leads_created + raw_leads_updated:
            lid = str(lead.get("id", ""))
            if lid and lid not in seen:
                seen.add(lid)
                raw_leads.append(lead)

        # Flatten básico primeiro
        result: list[dict[str, Any]] = [_flatten_lead(l) for l in raw_leads]

        # Enriquecer telefones via batch de contatos
        # (API Kommo não inclui custom_fields nos contatos embutidos em leads)
        leads_sem_fone = [r for r in result if not r["telefone"]]
        if leads_sem_fone:
            # Mapear contact_id → flat_lead para enrichment
            contact_to_lead_idx: dict[int, int] = {}
            for i, (flat, raw) in enumerate(zip(result, raw_leads)):
                if not flat["telefone"]:
                    for c in raw.get("_embedded", {}).get("contacts", []):
                        cid = c.get("id")
                        if cid:
                            contact_to_lead_idx[cid] = i

            if contact_to_lead_idx:
                contacts = self._client.get_contacts_by_ids(list(contact_to_lead_idx.keys()))
                for contact in contacts:
                    cid = contact.get("id")
                    phone = _phone_from_contact(contact)
                    if cid and phone:
                        idx = contact_to_lead_idx.get(cid)
                        if idx is not None and not result[idx]["telefone"]:
                            result[idx]["telefone"] = phone

        log.info(
            "kommo_leads_for_inspector",
            kommo_user_id=kommo_user_id,
            days_back=days_back,
            total=len(result),
        )
        return result

    def get_leads_em_atendimento(
        self,
        kommo_user_id: int,
    ) -> list[dict[str, Any]]:
        """
        Lista leads em "em atendimento" (status_id=10000005) de uma corretora.

        Mesmo schema de get_leads_for_inspector. Usado pelo Inspector v2 pra
        cobrir leads ativos mesmo sem atividade nas últimas 24h.

        Args:
            kommo_user_id: responsible_user_id da corretora

        Returns:
            list[dict] achatado, telefones enriquecidos via batch de contatos.
        """
        STATUS_EM_ATENDIMENTO = 10000005

        base_filters: dict[str, Any] = {
            "filter[pipeline_id]": self._pipeline_id,
            "filter[statuses][0][pipeline_id]": self._pipeline_id,
            "filter[statuses][0][status_id]": STATUS_EM_ATENDIMENTO,
            "filter[responsible_user_id][]": kommo_user_id,
            "with": "contacts",
        }

        raw_leads = list(self._client.get_leads(base_filters))

        # Defesa: descarta qualquer lead que escape do pipeline/status alvo
        raw_leads = [
            l for l in raw_leads
            if l.get("pipeline_id") == self._pipeline_id
            and l.get("status_id") == STATUS_EM_ATENDIMENTO
        ]

        result: list[dict[str, Any]] = [_flatten_lead(l) for l in raw_leads]

        # Enriquecer telefones via batch de contatos (mesmo padrão de get_leads_for_inspector)
        leads_sem_fone = [r for r in result if not r["telefone"]]
        if leads_sem_fone:
            contact_to_lead_idx: dict[int, int] = {}
            for i, (flat, raw) in enumerate(zip(result, raw_leads)):
                if not flat["telefone"]:
                    for c in raw.get("_embedded", {}).get("contacts", []):
                        cid = c.get("id")
                        if cid:
                            contact_to_lead_idx[cid] = i

            if contact_to_lead_idx:
                contacts = self._client.get_contacts_by_ids(list(contact_to_lead_idx.keys()))
                for contact in contacts:
                    cid = contact.get("id")
                    phone = _phone_from_contact(contact)
                    if cid and phone:
                        idx = contact_to_lead_idx.get(cid)
                        if idx is not None and not result[idx]["telefone"]:
                            result[idx]["telefone"] = phone

        log.info(
            "kommo_leads_em_atendimento",
            kommo_user_id=kommo_user_id,
            total=len(result),
        )
        return result

    def get_pipeline_stages(self) -> str:
        """
        Retorna mapa de etapas do pipeline da Acme.

        Returns:
            JSON com {stage_id: stage_name} para resolver IDs em nomes legíveis.
        """
        import json
        data = self._client.get_pipeline(self._pipeline_id)
        statuses = data.get("_embedded", {}).get("statuses", [])
        stage_map = {str(s["id"]): s["name"] for s in statuses if "id" in s and "name" in s}
        log.info("kommo_pipeline_stages", count=len(stage_map))
        return json.dumps(stage_map, ensure_ascii=False)

    # ── Mutações (Recovery) ─────────────────────────────────────────────────

    def move_lead_stage(
        self,
        lead_id: str,
        status_id: int,
        pipeline_id: int | None = None,
    ) -> str:
        """
        Move lead pra outra etapa do funil.

        ⚠️  Respeita DRY_RUN: em modo dry, apenas loga sem chamar a API.

        Args:
            lead_id: ID do lead no Kommo.
            status_id: ID da etapa destino.
            pipeline_id: opcional (default: pipeline configurado).

        Returns:
            JSON {"ok": true, "dry_run": bool, "lead_id", "status_id"}.
        """
        import json
        target_pipeline = pipeline_id if pipeline_id is not None else self._pipeline_id

        if settings.dry_run:
            log.info(
                "kommo_move_stage_dryrun",
                lead_id=lead_id,
                status_id=status_id,
                pipeline_id=target_pipeline,
            )
            return json.dumps({
                "ok": True, "dry_run": True,
                "lead_id": lead_id, "status_id": status_id,
            })

        self._client.update_lead_stage(lead_id, status_id, pipeline_id=target_pipeline)
        log.info(
            "kommo_move_stage_done",
            lead_id=lead_id, status_id=status_id, pipeline_id=target_pipeline,
        )
        return json.dumps({
            "ok": True, "dry_run": False,
            "lead_id": lead_id, "status_id": status_id,
        })

    def add_lead_note(self, lead_id: str, text: str) -> str:
        """
        Adiciona nota textual a um lead (timeline do Kommo).

        ⚠️  Respeita DRY_RUN: em modo dry, apenas loga sem chamar a API.

        Args:
            lead_id: ID do lead.
            text: corpo da nota.

        Returns:
            JSON {"ok": true, "dry_run": bool, "lead_id", "chars"}.
        """
        import json
        if settings.dry_run:
            log.info(
                "kommo_add_note_dryrun",
                lead_id=lead_id, chars=len(text), preview=text[:120],
            )
            return json.dumps({
                "ok": True, "dry_run": True,
                "lead_id": lead_id, "chars": len(text),
            })

        self._client.add_lead_note(lead_id, text)
        log.info("kommo_add_note_done", lead_id=lead_id, chars=len(text))
        return json.dumps({
            "ok": True, "dry_run": False,
            "lead_id": lead_id, "chars": len(text),
        })
