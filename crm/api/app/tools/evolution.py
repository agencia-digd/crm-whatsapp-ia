"""
EvolutionTool — Agno Toolkit para leitura e envio via Evolution API.

Fase 3A: leitura de conversas.
Fase 3B: envio self-message (instância só envia pro próprio número da corretora).

Regras de privacidade:
- Conteúdo de mensagem NUNCA vai para log
- Grupos (@g.us) são filtrados — só conversas 1:1
- Stickers e reactions são ignorados
- send_to_self: guard-rail verifica ownerJid contra telefone no banco antes de enviar
"""
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from agno.tools import Toolkit

from app.config import is_test_or_dryrun, settings
from app.db.models import BriefingSent, Corretora
from app.db.session import SessionLocal
from app.integrations.evolution_client import EvolutionClient

log = structlog.get_logger()

# Sufixos de JID para contatos individuais (não-grupos).
# @s.whatsapp.net → formato clássico (padrão iPhone / número de telefone)
# @lid → Linked Device ID, formato Android/Multi-Device (opaco, sem telefone no JID)
# Ref: https://github.com/EvolutionAPI/evolution-api/issues/1916
_VALID_INDIVIDUAL_SUFFIXES = ("@s.whatsapp.net", "@lid")

# Tipos de mensagem que carregam texto legível
_TEXT_TYPES = {"conversation", "extendedTextMessage", "imageMessage", "videoMessage",
               "documentMessage", "audioMessage", "pttMessage", "documentWithCaptionMessage"}

# Tipos para pular completamente
_SKIP_TYPES = {"stickerMessage", "reactionMessage", "protocolMessage", "pollCreationMessage",
               "pollUpdateMessage", "viewOnceMessage", "viewOnceMessageV2",
               "ephemeralMessage", "liveLocationMessage"}


def _extract_text(message: dict[str, Any], msg_type: str) -> str:
    """Extrai texto legível de uma mensagem sem logar o conteúdo."""
    if msg_type == "conversation":
        return str(message.get("conversation", ""))
    if msg_type == "extendedTextMessage":
        return str(message.get("extendedTextMessage", {}).get("text", ""))
    if msg_type in {"imageMessage", "videoMessage", "documentWithCaptionMessage",
                    "documentMessage"}:
        inner = message.get(msg_type, {})
        return str(inner.get("caption", ""))
    return ""


# Mapa de rank de progressão do ACK do WhatsApp (Baileys/Evolution).
# O array `MessageUpdate` de cada mensagem `fromMe` acumula TODOS os updates de
# status recebidos — NÃO ordenado por progressão (READ pode aparecer no meio,
# seguido de novos DELIVERY_ACK). Por isso pegamos o status de MAIOR rank.
# Valores REAIS observados nas instâncias acme_corretor/acme_corretora_c (2026-07-08):
# DELIVERY_ACK, READ, PLAYED, SERVER_ACK, EDITED (EDITED = edição, não é ACK).
_ACK_RANK: dict[str, int] = {
    "PENDING": 0,
    "ERROR": 0,
    "SERVER_ACK": 1,   # servidor recebeu → "enviada" (✓)
    "DELIVERY_ACK": 2,  # entregue no aparelho → "entregue" (✓✓)
    "READ": 3,          # lida (✓✓)
    "PLAYED": 3,        # áudio ouvido — equivale a lida (✓✓)
}

# ACK bruto → delivery_status do contrato do front ("sent"|"delivered"|"read"|"failed").
_ACK_TO_DELIVERY: dict[str, str] = {
    "SERVER_ACK": "sent",
    "DELIVERY_ACK": "delivered",
    "READ": "read",
    "PLAYED": "read",
    "ERROR": "failed",
}


def _extract_delivery_status(raw: dict[str, Any]) -> str | None:
    """
    Deriva o status de entrega de uma mensagem `fromMe` a partir do array
    ``MessageUpdate`` que a Evolution/Baileys devolve no find_messages.

    O array acumula updates fora de ordem — pega o de MAIOR rank de progressão
    (ver ``_ACK_RANK``) e mapeia pro contrato do front. Se não houver update
    algum (array vazio/ausente), a mensagem foi enviada mas ainda sem ACK
    registrado → "sent" (mostra 1 check).

    Returns:
        "sent" | "delivered" | "read" | "failed" ou None se indeterminado.
    """
    updates = raw.get("MessageUpdate")
    best_rank = -1
    best_ack: str | None = None
    if isinstance(updates, list):
        for upd in updates:
            if not isinstance(upd, dict):
                continue
            ack = upd.get("status")
            if not isinstance(ack, str):
                continue
            rank = _ACK_RANK.get(ack)
            if rank is None:
                continue  # ex: EDITED — não é ACK de entrega
            if rank > best_rank:
                best_rank = rank
                best_ack = ack
    if best_ack is not None:
        # ERROR tem rank 0 mas mapeia pra "failed" (não pra "sent")
        return _ACK_TO_DELIVERY.get(best_ack)
    # Sem nenhum ACK conhecido no array: assume enviada (foi fromMe).
    return "sent"


def _classify_type(msg_type: str) -> str:
    """Normaliza tipo da mensagem para o schema interno."""
    if msg_type in {"conversation", "extendedTextMessage"}:
        return "text"
    if msg_type == "imageMessage":
        return "image"
    if msg_type in {"audioMessage", "pttMessage"}:
        return "audio"
    if msg_type in {"documentMessage", "documentWithCaptionMessage"}:
        return "document"
    if msg_type == "videoMessage":
        return "video"
    return "other"


def _phone_from_jid(jid: str) -> str:
    """Extrai número de telefone de um JID. Ex: '5500000000000...@s.whatsapp.net' → '5500000000000...'"""
    return jid.split("@")[0]


def _normalize_message(raw: dict[str, Any]) -> dict[str, Any] | None:
    """
    Converte mensagem bruta da Evolution API para schema interno.

    Returns:
        Dict normalizado ou None se deve ser ignorado.
    """
    msg_type: str = raw.get("messageType", "")

    if msg_type in _SKIP_TYPES:
        return None

    key = raw.get("key", {})
    from_me: bool = bool(key.get("fromMe", False))
    remote_jid: str = key.get("remoteJid", "")
    msg_id: str = key.get("id", "")

    # Ignorar grupos
    if remote_jid.endswith("@g.us"):
        return None

    # Mensagem deletada — protocolMessage já filtrada acima, mas status pode indicar
    message_payload = raw.get("message", {})
    if not message_payload:
        deleted = {"from": "corretora" if from_me else "lead",
                   "text": "[mensagem deletada]",
                   "at": _ts_to_iso(raw.get("messageTimestamp")),
                   "type": "deleted", "id": msg_id}
        if from_me:
            deleted["delivery_status"] = _extract_delivery_status(raw)
        return deleted

    text = _extract_text(message_payload, msg_type)
    classified = _classify_type(msg_type)

    ts = raw.get("messageTimestamp")

    normalized: dict[str, Any] = {
        "from": "corretora" if from_me else "lead",
        "text": text,
        "at": _ts_to_iso(ts),
        "type": classified,
        "id": msg_id,  # key.id — usado pra baixar mídia (áudio) sob demanda
    }
    # Documento/vídeo: nome original e mimetype, pro chat mostrar "📎
    # contrato.pdf" clicável em vez de um rótulo "documento" sem ação.
    if classified in {"document", "video", "image"}:
        inner = (
            message_payload.get("documentMessage")
            or message_payload.get("documentWithCaptionMessage")
            or message_payload.get("videoMessage")
            or message_payload.get("imageMessage")
            or {}
        )
        nome = inner.get("fileName") or inner.get("title")
        if nome:
            normalized["filename"] = str(nome)
        if inner.get("mimetype"):
            normalized["mime"] = str(inner["mimetype"])
    # Status de entrega (✓/✓✓/❌) só faz sentido nas mensagens da corretora
    # (fromMe) — o front só renderiza DeliveryBadge nelas.
    if from_me:
        normalized["delivery_status"] = _extract_delivery_status(raw)
    return normalized


def _ts_to_iso(ts: Any) -> str:
    """Converte Unix timestamp (int ou str) para ISO 8601 UTC."""
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return str(ts)


class EvolutionTool(Toolkit):
    """
    Toolkit para leitura de conversas WhatsApp via Evolution API.

    Exposta ao Lead Inspector e aos Agentes (read-only nesta fase).
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            name="evolution_tool",
            tools=[
                self.check_instance_status,
                self.list_recent_chats,
                self.get_conversation,
                self.send_to_self,
            ],
            **kwargs,
        )
        self._client = EvolutionClient()

    # ── Exposed tools ─────────────────────────────────────────────────────────

    def check_instance_status(self, instance: str) -> str:
        """
        Verifica estado de conexão de uma instância Evolution.

        Args:
            instance: nome da instância (ex: 'acme_corretora_a')

        Returns:
            'open' | 'connecting' | 'close' | 'unknown'
        """
        status = self._client.get_instance_status(instance)
        log.info("evolution_instance_status", instance=instance, status=status)
        if status == "open":
            self._stamp_connected_since(instance)
        return status

    def _stamp_connected_since(self, instance: str) -> None:
        """Grava evolution_connected_since na 1ª vez que a instância fica 'open'."""
        try:
            with SessionLocal() as session:
                corretora = (
                    session.query(Corretora)
                    .filter(Corretora.evolution_instance == instance)
                    .first()
                )
                if corretora is not None and corretora.evolution_connected_since is None:
                    corretora.evolution_connected_since = datetime.now(timezone.utc)
                    session.commit()
                    log.info(
                        "evolution_connected_since_stamped",
                        instance=instance,
                        corretora_id=corretora.id,
                    )
        except Exception:
            log.exception("evolution_connected_since_stamp_failed", instance=instance)

    def list_recent_chats(
        self,
        instance: str,
        since_hours: int = 24,
    ) -> list[dict[str, Any]]:
        """
        Lista chats 1:1 com atividade recente.

        Args:
            instance: nome da instância Evolution
            since_hours: janela de tempo em horas (default 24)

        Returns:
            Lista de [{remote_jid, phone, contact_name, last_msg_at, unread_count}]
            Grupos são excluídos automaticamente.
        """
        since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        raw_chats = self._client.find_chats(instance, since=since)

        result: list[dict[str, Any]] = []
        for chat in raw_chats:
            jid: str = chat.get("remoteJid", "")
            if not jid.endswith(_VALID_INDIVIDUAL_SUFFIXES):
                continue  # ignora grupos (@g.us) e broadcasts

            last_msg = chat.get("lastMessage", {}) or {}
            last_key = last_msg.get("key", {}) if isinstance(last_msg, dict) else {}
            last_ts = last_msg.get("messageTimestamp") if isinstance(last_msg, dict) else None

            # Para @lid, o JID não contém o telefone. Tenta extrair via remoteJidAlt
            # que a Evolution API inclui na lastMessage para compatibilidade.
            if jid.endswith("@lid") and isinstance(last_key, dict):
                alt = last_key.get("remoteJidAlt", "")
                phone = alt.split("@")[0] if alt.endswith("@s.whatsapp.net") else _phone_from_jid(jid)
            else:
                phone = _phone_from_jid(jid)

            result.append({
                "remote_jid": jid,
                "phone": phone,
                "contact_name": chat.get("pushName") or "",
                "last_msg_at": _ts_to_iso(last_ts) if last_ts else chat.get("updatedAt", ""),
                "unread_count": chat.get("unreadCount", 0),
            })

        # Fix 1: dedup por remote_jid — mesmo contato pode aparecer como
        # @s.whatsapp.net E @lid; mantém o entry com last_msg_at mais recente.
        seen: dict[str, dict[str, Any]] = {}
        for item in result:
            k = item["remote_jid"]
            if k not in seen or item["last_msg_at"] > seen[k]["last_msg_at"]:
                seen[k] = item
        len_antes = len(result)
        result = list(seen.values())
        if len_antes != len(result):
            log.info(
                "evolution_chats_dedup",
                instance=instance,
                before=len_antes,
                after=len(result),
            )

        log.info(
            "evolution_chats_listed",
            instance=instance,
            since_hours=since_hours,
            total=len(result),
        )
        return result

    def get_conversation(
        self,
        instance: str,
        lead_phone: str,
        hours_back: int = 24,
    ) -> list[dict[str, Any]]:
        """
        Retorna mensagens de uma conversa em ordem cronológica.

        Args:
            instance: nome da instância Evolution
            lead_phone: telefone do lead em dígitos (ex: '+5500000000000') ou
                        JID completo (@s.whatsapp.net ou @lid)
            hours_back: quantas horas atrás buscar (default 24)

        Returns:
            Lista cronológica de:
            [{"from": "lead"|"corretora", "text": str, "at": ISO, "type": str}]
            Conteúdo de mensagem NÃO é logado.

        Nota:
            No Multi-Device o mesmo lead costuma ter a conversa PARTIDA entre
            dois JIDs na mesma instância: ``@s.whatsapp.net`` (clássico) e
            ``@lid`` (opaco). O split é frequentemente correlacionado com a
            direção — parte das mensagens do lead cai num JID e as respostas da
            corretora (fromMe) no outro. Por isso NÃO basta ler o primeiro JID
            não-vazio: as mensagens do outro JID (tipicamente as que a corretora
            mandou pelo próprio WhatsApp) sumiriam da thread. A leitura busca os
            DOIS JIDs e faz merge com dedup por ``key.id``.
        """
        # Aceita JID completo passado diretamente (qualquer sufixo suportado)
        if lead_phone.endswith(_VALID_INDIVIDUAL_SUFFIXES):
            jids: list[str] = [lead_phone]
            # Se veio um @s.whatsapp.net, tenta somar o @lid do mesmo telefone.
            if lead_phone.endswith("@s.whatsapp.net"):
                digits = lead_phone.replace("@s.whatsapp.net", "")
                lid_jid = self._find_jid_by_phone(instance, digits)
                if lid_jid and lid_jid not in jids:
                    jids.append(lid_jid)
        else:
            digits = lead_phone.lstrip("+").replace(" ", "").replace("-", "")
            jids = [f"{digits}@s.whatsapp.net"]
            # Resolve o @lid do mesmo telefone e lê os dois — a conversa vive
            # partida entre eles no Multi-Device.
            lid_jid = self._find_jid_by_phone(instance, digits)
            if lid_jid and lid_jid not in jids:
                jids.append(lid_jid)

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

        # Merge dos dois JIDs com dedup por key.id (evita contar 2x uma mensagem
        # que a Evolution devolva sob os dois JIDs).
        by_id: dict[str, dict[str, Any]] = {}
        for jid in jids:
            for raw in self._client.find_messages(instance, jid, limit=100):
                ts = raw.get("messageTimestamp")
                if ts:
                    try:
                        msg_dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                        if msg_dt < cutoff:
                            continue
                    except (ValueError, OSError, OverflowError):
                        pass

                normalized = _normalize_message(raw)
                if normalized is None:
                    continue
                mid = normalized.get("id") or ""
                # Sem id (raro): usa uma chave sintética pra não colapsar msgs distintas.
                key = mid or f"noid:{normalized.get('at','')}:{normalized.get('from','')}:{len(by_id)}"
                by_id.setdefault(key, normalized)

        # Ordem cronológica (mais antiga primeiro)
        messages = sorted(by_id.values(), key=lambda m: m.get("at", ""))

        log.info(
            "evolution_conversation_fetched",
            instance=instance,
            jids=len(jids),
            hours_back=hours_back,
            total_msgs=len(messages),
        )
        return messages

    def send_to_self(self, instance: str, text: str) -> bool:
        """
        Envia mensagem de texto para o próprio número da corretora dona da instância.

        Modo self-message MVP: a instância só pode enviar para o telefone
        cadastrado no banco para aquela instância, verificado contra o ownerJid
        reportado pela Evolution API. Qualquer divergência bloqueia o envio.

        Honra settings.dry_run: com DRY_RUN=true loga a intenção mas não chama a API.
        Toda tentativa (real ou dry-run) é registrada em briefings_sent.

        Args:
            instance: nome da instância Evolution (ex: 'acme_corretora_a')
            text: conteúdo da mensagem a enviar

        Returns:
            True se enviado com sucesso (ou dry-run), False em caso de bloqueio/erro
        """
        # 1. Buscar corretora no banco
        corretora_info = self._get_corretora_info(instance)
        if corretora_info is None:
            log.error("evolution_send_self_no_corretora", instance=instance)
            return False

        corretora_id: int = corretora_info["id"]
        corretora_phone: str = corretora_info["phone"]

        # 2. Verificar ownerJid da instância conectada
        owner_phone = self._get_instance_owner_phone(instance)
        if owner_phone is None:
            log.error("evolution_send_self_no_owner", instance=instance)
            self._record_briefing(corretora_id, text, success=False)
            return False

        # 3. Guard-rail: telefone do banco deve bater com o ownerJid da instância
        if corretora_phone != owner_phone:
            log.error(
                "evolution_send_self_phone_mismatch",
                instance=instance,
                corretora_last4=corretora_phone[-4:],
                owner_last4=owner_phone[-4:],
            )
            self._record_briefing(corretora_id, text, success=False)
            return False

        # 4. Dry-run: loga intenção, não chama API
        if settings.dry_run:
            log.info(
                "[DRY_RUN] evolution_send_self",
                instance=instance,
                to_last4=corretora_phone[-4:],
                length_chars=len(text),
            )
            self._record_briefing(corretora_id, text, success=True)
            return True

        # 5. Envio real
        try:
            self._client.send_text(instance, corretora_phone, text)
            self._record_briefing(corretora_id, text, success=True)
            log.info(
                "evolution_send_self_ok",
                instance=instance,
                to_last4=corretora_phone[-4:],
                length_chars=len(text),
            )
            return True
        except Exception:
            log.exception("evolution_send_self_failed", instance=instance)
            self._record_briefing(corretora_id, text, success=False)
            return False

    def send_to_pessoa(self, text: str) -> bool:
        """
        Envia mensagem pro WhatsApp da Pessoa Pessoa9 via instância de sistema.

        Destinatário paralelo do briefing diário (Onda 7 · Fase 3C). Lê
        `settings.pessoa_whatsapp` e `settings.system_instance`.

        Honra `settings.dry_run`: dry_run loga e retorna True sem chamar API.

        Returns:
            True se enviado com sucesso (ou dry_run), False se erro/desabilitado.
        """
        numero = settings.pessoa_whatsapp.strip()
        instance = settings.system_instance.strip()
        if not numero or not instance:
            log.info("pessoa_whatsapp_disabled", numero_set=bool(numero), instance_set=bool(instance))
            return False

        if settings.dry_run:
            log.info(
                "[DRY_RUN] evolution_send_pessoa",
                instance=instance,
                to_last4=numero[-4:],
                length_chars=len(text),
            )
            self._record_pessoa_briefing(text, success=True)
            return True

        try:
            self._client.send_text(instance, numero, text)
            self._record_pessoa_briefing(text, success=True)
            log.info(
                "evolution_send_pessoa_ok",
                instance=instance,
                to_last4=numero[-4:],
                length_chars=len(text),
            )
            return True
        except Exception as exc:
            log.exception("evolution_send_pessoa_failed", instance=instance, error=str(exc))
            self._record_pessoa_briefing(text, success=False)
            return False

    def _record_pessoa_briefing(self, conteudo: str, success: bool) -> None:
        from app.db.session import SessionLocal
        from app.db import models
        with SessionLocal() as session:
            session.add(models.BriefingSent(
                destinatario="pessoa",
                canal="whatsapp",
                conteudo=conteudo,
                success=success,
                is_test=is_test_or_dryrun(),
            ))
            session.commit()

    # ── Private helpers ───────────────────────────────────────────────────────

    def _find_jid_by_phone(self, instance: str, digits: str) -> str | None:
        """
        Resolve o JID correto (@s.whatsapp.net ou @lid) para um número de telefone.

        Varre todos os chats da instância:
        1. Retorna f"{digits}@s.whatsapp.net" se esse chat existir (formato clássico)
        2. Retorna o @lid JID se remoteJidAlt do lastMessage bater com o telefone
        3. Retorna None se não encontrar

        Necessário porque JIDs @lid são IDs opacos — não é possível derivar o
        telefone deles sem consultar a lista de chats.

        Para resolver múltiplos JIDs da mesma instância, prefira
        ``build_jid_lookup_map`` — chama ``find_chats`` 1x e devolve um dict
        ``digits → jid`` que cobre todos os leads de uma vez.
        """
        try:
            all_chats = self._client.find_chats(instance)
        except Exception:
            log.warning("find_jid_by_phone_find_chats_failed", instance=instance)
            return None

        classic_jid = f"{digits}@s.whatsapp.net"

        for chat in all_chats:
            jid: str = chat.get("remoteJid", "")
            if jid == classic_jid:
                return classic_jid
            if jid.endswith("@lid"):
                last_msg = chat.get("lastMessage") or {}
                if isinstance(last_msg, dict):
                    key = last_msg.get("key") or {}
                    if isinstance(key, dict) and key.get("remoteJidAlt") == classic_jid:
                        return jid

        return None

    def build_jid_lookup_map(self, instance: str) -> dict[str, str]:
        """
        Constrói um dicionário ``digits_telefone → jid`` para toda a instância
        em uma única chamada a ``find_chats``.

        Cobre os dois formatos do Multi-Device:
          • ``@s.whatsapp.net`` — chave = ``remoteJid.split("@")[0]``
          • ``@lid``            — chave = digits derivados de
            ``lastMessage.key.remoteJidAlt`` quando disponível

        Uso típico (Inspector): chamar 1x por corretora antes do loop de leads
        e passar o dict pra ``should_reanalyze`` ao invés de chamar
        ``_find_jid_by_phone`` por lead (que dispara ``find_chats`` toda vez).

        Em caso de erro de rede, retorna dict vazio — chamadores devem cair
        no fallback pra ``_find_jid_by_phone``.
        """
        try:
            all_chats = self._client.find_chats(instance)
        except Exception:
            log.warning("build_jid_lookup_map_failed", instance=instance)
            return {}

        result: dict[str, str] = {}
        for chat in all_chats:
            jid: str = chat.get("remoteJid", "")
            if not jid:
                continue
            if jid.endswith("@s.whatsapp.net"):
                digits = jid.split("@")[0]
                if digits:
                    result.setdefault(digits, jid)
            elif jid.endswith("@lid"):
                last_msg = chat.get("lastMessage") or {}
                if isinstance(last_msg, dict):
                    key = last_msg.get("key") or {}
                    if isinstance(key, dict):
                        alt = key.get("remoteJidAlt") or ""
                        if alt.endswith("@s.whatsapp.net"):
                            digits = alt.split("@")[0]
                            if digits:
                                result.setdefault(digits, jid)
        log.info("build_jid_lookup_map_ok", instance=instance, jids=len(result))
        return result

    def _get_corretora_info(self, instance: str) -> dict[str, Any] | None:
        """Retorna {id, phone} da corretora dona da instância, ou None."""
        try:
            with SessionLocal() as session:
                corretora = (
                    session.query(Corretora)
                    .filter(Corretora.evolution_instance == instance)
                    .first()
                )
                if corretora is None:
                    return None
                phone = corretora.telefone.lstrip("+").replace(" ", "").replace("-", "")
                return {"id": corretora.id, "phone": phone}
        except Exception:
            log.exception("evolution_get_corretora_info_failed", instance=instance)
            return None

    def _get_instance_owner_phone(self, instance: str) -> str | None:
        """Retorna o número conectado à instância via ownerJid, ou None."""
        try:
            instances = self._client.fetch_instances()
            for inst in instances:
                name = inst.get("name") or inst.get("instanceName")
                if name == instance:
                    owner_jid: str = inst.get("ownerJid", "")
                    if owner_jid:
                        return owner_jid.split("@")[0]
            return None
        except Exception:
            log.exception("evolution_get_owner_phone_failed", instance=instance)
            return None

    def _record_briefing(self, corretora_id: int, text: str, success: bool) -> None:
        """Registra tentativa de envio em briefings_sent para auditoria."""
        try:
            with SessionLocal() as session:
                record = BriefingSent(
                    destinatario=f"corretora:{corretora_id}",
                    canal="whatsapp_self",
                    conteudo=text,
                    success=success,
                    is_test=is_test_or_dryrun(),
                )
                session.add(record)
                session.commit()
        except Exception:
            log.exception("evolution_record_briefing_failed", corretora_id=corretora_id)
