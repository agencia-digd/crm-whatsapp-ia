"""
Persistência de mensagens WhatsApp em mensagens_whatsapp.

Regras:
- UPSERT com ON CONFLICT (evolution_message_id, instance) DO NOTHING — idempotente
- Nunca levanta — erros são logados, caller segue
- Conteúdo de mensagem NUNCA vai para log (privacidade)

Duas entradas públicas:
  gravar_mensagem_evolution_from_webhook(instance, payload)  → uuid.UUID | None (nova msg inserida)
  gravar_mensagem_evolution_from_evolution_msg(instance, raw_msg) → bool

gravar_mensagem_evolution é alias de from_webhook (backward compat).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import Corretora, MensagemWhatsapp
from app.db.session import SessionLocal

log = structlog.get_logger()

# Chave do payload → tipo normalizado
_TIPO_MAP: dict[str, str] = {
    "conversation": "text",
    "extendedTextMessage": "text",
    "audioMessage": "audio",
    "pttMessage": "audio",
    "videoMessage": "video",
    "imageMessage": "image",
    "documentMessage": "document",
    "documentWithCaptionMessage": "document",
    "stickerMessage": "sticker",
    "templateMessage": "text",
}


def _resolve_corretora_id(instance: str) -> int | None:
    with SessionLocal() as s:
        c = s.execute(
            select(Corretora).where(Corretora.evolution_instance == instance)
        ).scalar_one_or_none()
        return c.id if c else None


def _detect_tipo(msg: dict[str, Any]) -> str:
    for key in msg:
        if key in _TIPO_MAP:
            return _TIPO_MAP[key]
    return "unknown"


def _extract_template(
    template_msg: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    """
    Extrai texto e metadata de templateMessage.

    Estrutura Evolution (dois formatos vistos em produção):
      templateMessage.hydratedFourRowTemplate.*  → Baileys clássico
      templateMessage.hydratedTemplate.*         → HSM da linha Cloud/Mac (Pessoa12)

    Ambos carregam:
      hydratedContentText  → corpo
      hydratedTitleText    → título (geralmente vazio)
      hydratedButtons[]    → botões
      templateId           → ID do template no Meta

    O HSM da Pessoa12 (instância acme_corretora_a) grava em ``hydratedTemplate``;
    checar só ``hydratedFourRowTemplate`` deixava a conversa "cega" (I3 da
    Revisão 03 — combustível da conversa trocada I1).
    """
    hydrated = (
        template_msg.get("hydratedFourRowTemplate")
        or template_msg.get("hydratedTemplate")
        or {}
    )

    title = (hydrated.get("hydratedTitleText") or "").strip()
    body = (hydrated.get("hydratedContentText") or "").strip()

    parts = [p for p in [title, body] if p]
    texto = "\n".join(parts) or None

    buttons: list[dict[str, Any]] = hydrated.get("hydratedButtons") or []
    meta: dict[str, Any] = {
        "tem_botoes": len(buttons) > 0,
        "quantos_botoes": len(buttons),
    }
    if hydrated.get("templateId"):
        meta["template_id"] = hydrated["templateId"]

    return texto, meta


def _extract_content_meta(
    msg: dict[str, Any], tipo: str
) -> tuple[str | None, dict[str, Any]]:
    """Retorna (conteudo_texto, conteudo_metadata)."""
    # templateMessage precisa ser checado antes do bloco genérico tipo=="text",
    # pois também mapeia pra tipo=text mas tem extração própria
    if "templateMessage" in msg:
        return _extract_template(msg["templateMessage"])

    if tipo == "text":
        text = msg.get("conversation") or (msg.get("extendedTextMessage") or {}).get("text")
        return (text or None), {}

    if tipo == "audio":
        inner = msg.get("audioMessage") or msg.get("pttMessage") or {}
        meta: dict[str, Any] = {"ptt": bool(inner.get("ptt", False))}
        if inner.get("seconds") is not None:
            meta["duracao_segundos"] = inner["seconds"]
        if inner.get("mimetype"):
            meta["mimetype"] = inner["mimetype"]
        return None, meta

    if tipo == "video":
        inner = msg.get("videoMessage") or {}
        meta = {}
        if inner.get("seconds") is not None:
            meta["duracao_segundos"] = inner["seconds"]
        if inner.get("mimetype"):
            meta["mimetype"] = inner["mimetype"]
        if inner.get("fileLength"):
            meta["tamanho_bytes"] = inner["fileLength"]
        return (inner.get("caption") or None), meta

    if tipo == "image":
        inner = msg.get("imageMessage") or {}
        meta = {}
        if inner.get("mimetype"):
            meta["mimetype"] = inner["mimetype"]
        if inner.get("fileLength"):
            meta["tamanho_bytes"] = inner["fileLength"]
        return (inner.get("caption") or None), meta

    if tipo == "document":
        inner = msg.get("documentMessage") or msg.get("documentWithCaptionMessage") or {}
        meta = {}
        if inner.get("mimetype"):
            meta["mimetype"] = inner["mimetype"]
        if inner.get("fileLength"):
            meta["tamanho_bytes"] = inner["fileLength"]
        return (inner.get("caption") or None), meta

    return None, {}


def _ts_to_dt(ts: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _digits_from_jid_or_phone(value: str) -> str | None:
    """Extrai só dígitos de um JID (`5515...@s.whatsapp.net`) ou telefone cru.

    Ignora JIDs `@lid`/`@g.us`/`@broadcast` (não são telefone real).
    """
    if not value:
        return None
    if "@" in value:
        local, _, dominio = value.partition("@")
        if dominio in ("lid", "g.us", "broadcast"):
            return None
        value = local
    digitos = "".join(c for c in value if c.isdigit())
    return digitos or None


def extrair_telefone_lid(data: dict[str, Any]) -> str | None:
    """Tenta achar o telefone REAL de uma mensagem `@lid` no payload Evolution.

    JID `@lid` é opaco (privacy-preserving Multi-Device) — não dá pra derivar o
    telefone dele. Mas o Evolution v2 às vezes manda o telefone real num campo
    lateral. Ordem de tentativa (Evolution varia entre versões):
      1. data.key.remoteJidAlt
      2. data.key.senderPn
      3. data.key.participantAlt
      4. data.remoteJidAlt

    Retorna só os dígitos do telefone, ou None se nenhum campo tiver telefone real.
    """
    key = data.get("key") or {}
    candidatos = [
        key.get("remoteJidAlt"),
        key.get("senderPn"),
        key.get("participantAlt"),
        data.get("remoteJidAlt"),
    ]
    for cand in candidatos:
        if isinstance(cand, str):
            digitos = _digits_from_jid_or_phone(cand)
            if digitos:
                return digitos
    return None


# ── Core ──────────────────────────────────────────────────────────────────────

def _gravar_from_data(
    instance: str,
    data: dict[str, Any],
    raw_payload: dict[str, Any],
    *,
    return_id: bool = False,
) -> uuid.UUID | None | bool:
    """
    Extrai campos do dicionário `data` e persiste no banco.

    `data` pode ser:
      - payload["data"] vindo do webhook Evolution
      - um raw_msg vindo do find_messages (mesmo schema)

    Args:
        return_id: se True, retorna UUID da linha inserida (ou None se conflito/filtrado).
                   se False, retorna bool (True=inserida, False=conflito/filtrado).

    Levanta em caso de erro de DB — caller encapsula.
    """
    key = data.get("key") or {}
    remote_jid: str = key.get("remoteJid") or ""

    if not remote_jid or remote_jid.endswith(("@g.us", "@broadcast")):
        return False

    msg_id: str = key.get("id") or ""
    if not msg_id:
        return False

    msg: dict[str, Any] = data.get("message") or {}
    if not msg:
        return False

    from_me = bool(key.get("fromMe", False))
    tipo = _detect_tipo(msg)
    conteudo_texto, metadata = _extract_content_meta(msg, tipo)

    # Telefone real. Em JID `@lid` o local part é um ID opaco — tenta resolver o
    # telefone real do payload (remoteJidAlt/senderPn/...). Se não achar, mantém
    # comportamento antigo (grava o ID opaco) e loga pra visibilidade. `remote_jid`
    # original é sempre preservado pra rastreabilidade.
    if remote_jid.endswith("@lid"):
        telefone = extrair_telefone_lid(data)
        if telefone is None:
            # Payload não trouxe o alt. Fallback: consulta o Evolution DB
            # (Message.remoteJidAlt = mapping do Baileys). Best-effort, nunca levanta.
            from app.services.lid_resolver import resolver_lid_telefone

            telefone = resolver_lid_telefone(remote_jid)
        if telefone is None:
            telefone = remote_jid.split("@", 1)[0] or None
            log.info(
                "lid_sem_telefone",
                instance=instance,
                jid_suffix=remote_jid[-8:],
            )
    else:
        telefone = remote_jid.split("@", 1)[0] or None

    corretora_id = _resolve_corretora_id(instance)
    if corretora_id is None:
        log.warning("mensagens_evolution_instance_nao_mapeada", instance=instance)

    with SessionLocal() as s:
        stmt = (
            pg_insert(MensagemWhatsapp)
            .values(
                evolution_message_id=msg_id,
                instance=instance,
                corretora_id=corretora_id,
                remote_jid=remote_jid,
                contato_telefone=telefone,
                # pushName é o nome de perfil de QUEM ENVIOU. Em fromMe=true
                # (corretora → lead) isso é o nome da PRÓPRIA corretora, não do
                # lead — usá-lo como contato_nome polui o agrupamento (todas as
                # msgs enviadas pela corretora caíam sob o nome dela em vez do
                # lead destinatário). Só aproveitamos o pushName quando a msg veio
                # do lead (fromMe=false); no outbound deixamos None — o nome real
                # do lead é resolvido pelas msgs inbound / leads_snapshot (telefone).
                contato_nome=(data.get("pushName") or None) if not from_me else None,
                from_corretora=from_me,
                tipo_mensagem=tipo,
                conteudo_texto=conteudo_texto,
                conteudo_metadata=metadata,
                timestamp_evolution=_ts_to_dt(data.get("messageTimestamp")),
                raw_payload=raw_payload,
            )
            .on_conflict_do_nothing(constraint="uq_mensagem_instance")
            .returning(MensagemWhatsapp.id)
        )
        result = s.execute(stmt)
        s.commit()
        inserted_id: uuid.UUID | None = result.scalar_one_or_none()

    if inserted_id:
        log.info(
            "mensagens_evolution_gravada",
            instance=instance,
            tipo=tipo,
            from_corretora=from_me,
            jid_suffix=remote_jid[-8:],
        )

    if return_id:
        return inserted_id
    return inserted_id is not None


# ── Webhook path ──────────────────────────────────────────────────────────────

def _gravar_from_webhook(instance: str, payload: dict[str, Any]) -> uuid.UUID | None:
    """Extrai de payload webhook e chama _gravar_from_data. Levanta em erro."""
    if payload.get("event") != "messages.upsert":
        return None
    data = payload.get("data") or {}
    result = _gravar_from_data(instance, data, raw_payload=payload, return_id=True)
    return result if isinstance(result, uuid.UUID) else None


def gravar_mensagem_evolution_from_webhook(
    instance: str, payload: dict[str, Any]
) -> uuid.UUID | None:
    """
    Persiste mensagem a partir de payload webhook Evolution. Nunca levanta.

    Returns UUID da mensagem se nova, None se já existia ou filtrada.
    """
    try:
        return _gravar_from_webhook(instance, payload)
    except Exception:
        log.error(
            "mensagens_evolution_gravar_failed",
            instance=instance,
            evt=payload.get("event"),
        )
        return None


# Alias backward compat — webhook importa esse nome
gravar_mensagem_evolution = gravar_mensagem_evolution_from_webhook


# ── find_messages path ────────────────────────────────────────────────────────

def _gravar_from_evolution_msg(instance: str, raw_msg: dict[str, Any]) -> bool:
    """
    Extrai de raw_msg (formato find_messages) e chama _gravar_from_data.

    O schema de raw_msg é idêntico a payload["data"] do webhook:
    {key: {remoteJid, id, fromMe}, message: {...}, messageTimestamp, pushName}

    Levanta em erro de DB.
    """
    return _gravar_from_data(instance, raw_msg, raw_payload=raw_msg)


def gravar_mensagem_evolution_from_evolution_msg(instance: str, raw_msg: dict[str, Any]) -> bool:
    """
    Persiste mensagem a partir de raw_msg do find_messages. Nunca levanta.

    Returns True se nova, False se já existia ou filtrada.
    """
    try:
        return _gravar_from_evolution_msg(instance, raw_msg)
    except Exception:
        jid = str((raw_msg.get("key") or {}).get("remoteJid") or "")
        log.error(
            "mensagens_evolution_gravar_msg_failed",
            instance=instance,
            jid_suffix=jid[-8:] if jid else "??",
        )
        return False
