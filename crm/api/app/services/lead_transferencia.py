"""Transferência de lead entre corretoras — o miolo do `POST /painel/crm/transferir`.

Extraído do handler `painel_crm_transferir` (05/09/2026) pra ser reusado pelo
handoff automático do remarketing (`handoff_remarketing`). O handler continua
dono da auth/escopo; aqui é SÓ a mutação, com as mesmas regras:

  • muda o dono nas 3 fontes de escopo:
      - leads.owner_id + owner_editado_manual (o shadow do Kommo NÃO desfaz);
      - leads_snapshot.corretora_id + corretora_editado_manual (o inspector NÃO
        desfaz);
      - lead_analyses.corretora_id (raio-X / coaching / frios);
  • grava lead_events tipo='transferencia' payload {de, para, quem, motivo,
    de_label, para_label, ...extra} — o kanban usa esse evento pro selo;
  • ZERO WhatsApp — nenhum aviso sai daqui;
  • NÃO escreve de volta no Kommo;
  • NÃO faz commit: quem abriu a sessão fecha.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog
from pydantic import BaseModel
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.db.models import Lead
from app.services.lead_spine import _registrar_evento

log = structlog.get_logger()

EVENTO_TRANSFERENCIA = "transferencia"


class Transferencia(BaseModel):
    lead_id: int
    de: int | None
    para: int
    de_label: str | None = None
    para_label: str | None = None
    stage_de: int | None = None
    stage_para: int | None = None


def label_corretora(session: Session, corretora_id: int | None) -> str | None:
    """Mesmo rótulo curto do painel (`CORRETORA_LABEL`): apelido, senão o
    1º nome. Lido do banco pra não importar `app.api.painel` num service."""
    if corretora_id is None:
        return None
    # `apelido` só existe em produção (não está no model nem em migration) —
    # o SAVEPOINT isola a falha pra não abortar a transação da transferência.
    try:
        with session.begin_nested():
            row = session.execute(
                sql_text(
                    "SELECT coalesce(nullif(apelido, ''), split_part(nome, ' ', 1)) "
                    "FROM corretoras WHERE id = :id"
                ),
                {"id": int(corretora_id)},
            ).scalar()
    except Exception:  # noqa: BLE001
        try:
            row = session.execute(
                sql_text("SELECT split_part(nome, ' ', 1) FROM corretoras WHERE id = :id"),
                {"id": int(corretora_id)},
            ).scalar()
        except Exception as e:  # noqa: BLE001
            log.warning("label_corretora_indisponivel", erro=str(e)[:120])
            return None
    return str(row) if row else None


def transferir_lead(
    session: Session,
    lead: Lead,
    destino_cid: int,
    *,
    de_cid: int | None,
    quem: str,
    motivo: str | None,
    de_label: str | None = None,
    para_label: str | None = None,
    stage_para: int | None = None,
    marcar_remarketing: bool = False,
    extra: dict[str, Any] | None = None,
) -> Transferencia:
    """Aplica a transferência no lead já carregado. Não valida escopo nem
    destino (isso é do chamador) e não faz commit.

    `de_cid` é o dono ATUAL já normalizado pra corretora_id interno (o handler
    resolve kommo_user_id → id antes de chamar). `stage_para` opcional: o
    handoff automático leva o card pro funil principal; o botão do painel
    mantém a etapa.

    `marcar_remarketing`: carimba `leads.veio_remarketing_em` — a tag
    "remarketing" que a corretora vê no card (pedido Dona 15/09/2026). Só o
    handoff automático liga isso; o botão do painel move lead de qualquer
    procedência e não pode inventar origem. Escrito UMA vez (coalesce): a
    primeira vez que o lead saiu do remarketing é a data que vale.
    """
    owner_anterior = int(lead.owner_id) if lead.owner_id is not None else None
    stage_anterior = int(lead.stage_id) if lead.stage_id is not None else None
    if de_label is None:
        de_label = label_corretora(session, de_cid)
    if para_label is None:
        para_label = label_corretora(session, destino_cid)

    lead.owner_id = destino_cid
    lead.owner_editado_manual = True
    if stage_para is not None:
        lead.stage_id = stage_para
    agora = datetime.now(timezone.utc)
    if marcar_remarketing and lead.veio_remarketing_em is None:
        lead.veio_remarketing_em = agora
    lead.updated_at = agora

    payload: dict[str, Any] = {
        "de": de_cid,
        "de_owner_id": owner_anterior,
        "para": destino_cid,
        "quem": quem,
        "motivo": motivo,
        "de_label": de_label,
        "para_label": para_label,
    }
    if stage_para is not None:
        payload["stage_de"] = stage_anterior
        payload["stage_para"] = stage_para
    if extra:
        payload.update(extra)
    _registrar_evento(session, int(lead.tenant_id), int(lead.id),
                      EVENTO_TRANSFERENCIA, payload)

    # Escopo da thread/coaching acompanha o novo dono. external_ref é o
    # lead_id do Kommo (chave de leads_snapshot e lead_analyses).
    if lead.external_ref:
        session.execute(
            sql_text(
                "UPDATE leads_snapshot SET corretora_id = :cid, "
                "corretora_editado_manual = TRUE WHERE lead_id = :lid"
            ),
            {"cid": destino_cid, "lid": str(lead.external_ref)},
        )
        session.execute(
            sql_text(
                "UPDATE lead_analyses SET corretora_id = :cid "
                "WHERE lead_id = :lid"
            ),
            {"cid": destino_cid, "lid": str(lead.external_ref)},
        )

    return Transferencia(
        lead_id=int(lead.id), de=de_cid, para=destino_cid,
        de_label=de_label, para_label=para_label,
        stage_de=stage_anterior,
        stage_para=stage_para if stage_para is not None else stage_anterior,
    )
