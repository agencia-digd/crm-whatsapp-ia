"""
Lê histórico de conversa do Postgres (mensagens_whatsapp) em vez da Evolution API.

Retorna formato estendido do EvolutionTool.get_conversation:
    [{"from": "lead"|"corretora", "text": str, "at": ISO, "type": str,
      "transcricao": str|None, "transcricao_status": str|None}]

Campos extras presentes apenas quando tipo_mensagem='audio':
  transcricao        — texto transcrito (None se não-áudio ou não transcrito)
  transcricao_status — 'concluida'|'pendente'|'em_progresso'|'erro'|None

Estratégia de lookup:
  1. Tenta JID @s.whatsapp.net direto (cobre ~5% dos leads, match exato)
  2. Se vazio: resolve @lid via Evolution.find_chats → query Postgres por remote_jid

O passo 2 mantém uma chamada à Evolution API para resolução de JID (@lid).
Isso será eliminado quando uma migration normalizar contato_telefone
para armazenar o telefone real em vez do ID opaco @lid.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import text

from app.db.session import SessionLocal

if TYPE_CHECKING:
    from app.tools.evolution import EvolutionTool

log = structlog.get_logger()

# mensagens_whatsapp.tipo_mensagem → tipo interno Inspector
_TYPE_MAP: dict[str, str] = {
    "text": "text",
    "audio": "audio",
    "image": "image",
    "video": "video",
    "document": "document",
    "sticker": "other",
    "unknown": "other",
}


def _normalize_phone(phone: str) -> str:
    """Converte qualquer formato de telefone para dígitos puros."""
    return phone.lstrip("+").replace(" ", "").replace("-", "")


def _query_by_jid(
    corretora_id: int,
    jid: str,
    cutoff: datetime,
) -> list[dict[str, Any]]:
    """Query mensagens_whatsapp por remote_jid exato, filtrando mensagens enviadas pelo robô (Corretor)."""
    with SessionLocal() as s:
        rows = s.execute(
            text("""
                SELECT me.from_corretora, me.conteudo_texto, me.timestamp_evolution,
                       me.tipo_mensagem, me.transcricao, me.transcricao_status
                FROM mensagens_whatsapp me
                LEFT JOIN recovery_messages rm
                  ON rm.lead_id = :jid
                 AND ABS(EXTRACT(EPOCH FROM (me.timestamp_evolution - rm.ts))) < 5
                WHERE me.corretora_id = :corretora_id
                  AND me.remote_jid = :jid
                  AND me.timestamp_evolution >= :cutoff
                  AND rm.id IS NULL
                ORDER BY me.timestamp_evolution ASC
            """),
            {"corretora_id": corretora_id, "jid": jid, "cutoff": cutoff},
        ).fetchall()

    return [
        {
            "from": "corretora" if r[0] else "lead",
            "text": r[1] or "",
            "at": r[2].isoformat() if r[2] else "",
            "type": _TYPE_MAP.get(r[3] or "", "other"),
            "transcricao": r[4] if r[3] == "audio" else None,
            "transcricao_status": r[5] if r[3] == "audio" else None,
        }
        for r in rows
    ]


def get_conversation_db(
    corretora_id: int,
    lead_phone: str,
    instance: str,
    evolution: EvolutionTool,
    hours_back: int = 72,
    cutoff_dt: datetime | None = None,
) -> list[dict[str, Any]]:
    """
    Busca histórico de conversa no Postgres.

    Substitui EvolutionTool.get_conversation no Inspector.
    Retorna formato estendido: [{"from", "text", "at", "type", "transcricao", "transcricao_status"}].

    cutoff_dt — quando fornecido, usa este datetime como corte em vez de hours_back.
    Isso permite que o pipeline passe a janela dinâmica da última execução.
    _apply_connection_window() no pipeline ainda é chamado para filtrar mensagens
    anteriores a evolution_connected_since.
    """
    digits = _normalize_phone(lead_phone)
    # cutoff_dt=None + hours_back<=0 = busca TUDO (usado pelo pipeline Fase 7.1
    # pra distinguir sem_atividade de sem_dado_historico).
    if cutoff_dt is not None:
        cutoff = cutoff_dt
    elif hours_back <= 0:
        cutoff = datetime(1970, 1, 1, tzinfo=timezone.utc)
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    # Tentativa 1: JID @s.whatsapp.net direto
    jid_classic = f"{digits}@s.whatsapp.net"
    msgs = _query_by_jid(corretora_id, jid_classic, cutoff)
    if msgs:
        log.info(
            "conversation_db_s_hit",
            corretora_id=corretora_id,
            total_msgs=len(msgs),
        )
        return msgs

    # Tentativa 2: resolve @lid via Evolution, busca no Postgres pelo JID resolvido
    lid_jid = evolution._find_jid_by_phone(instance, digits)
    if lid_jid and lid_jid != jid_classic:
        msgs = _query_by_jid(corretora_id, lid_jid, cutoff)
        log.info(
            "conversation_db_lid_hit",
            corretora_id=corretora_id,
            jid_suffix=lid_jid[-10:],
            total_msgs=len(msgs),
        )
        return msgs

    log.info(
        "conversation_db_miss",
        corretora_id=corretora_id,
        phone_suffix=digits[-4:] if len(digits) >= 4 else digits,
        hours_back=hours_back,
    )
    return []
