"""
Orquestração do fluxo de uma mensagem na conversa Corretor ↔ lead.

Entrada: mensagem do lead chegou via Evolution.
Saída: mensagem da Corretor enviada + estado persistido + (se terminal) Kommo
       atualizado (nota + etapa).

Mantido síncrono — webhook FastAPI roda em threadpool. Sem async/await
internamente pra não esbarrar no anti-pattern do CLAUDE.md (subprocess
síncrono em endpoint async trava o uvicorn).
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import SessionLocal, engine
from app.integrations.evolution_client import EvolutionClient
from app.llm.cli_provider import ClaudeCliProvider
from app.recovery import repo
from app.recovery.conversation import State, is_terminal
from app.recovery.extractor import QualificationResult, extract
from app.recovery.responder import generate_reply
from app.tools.kommo import KommoTool

log = structlog.get_logger()


@dataclass
class ProcessResult:
    sent: bool
    skipped_reason: str | None = None
    reply_text: str | None = None
    state: str | None = None


# ── Serialização por lead (Codex F5) ─────────────────────────────────────────
#
# Duas mensagens DIFERENTES do mesmo lead que chegam quase juntas eram
# processadas em paralelo: ambas liam o mesmo estado, chamavam o LLM e enviavam
# resposta, e o último commit sobrescrevia turns/estado da outra (a
# idempotência por message_id no webhook só cobre REENTREGA da MESMA mensagem).
#
# Fix: advisory lock de SESSÃO do Postgres por lead, BLOQUEANTE
# (`pg_advisory_lock`, não `try`). Escolha do bloqueante sobre try-com-skip: a
# 2ª mensagem é distinta e precisa ser respondida — descartá-la perderia um
# turno real do lead. Bloqueando, a 2ª espera a 1ª terminar e então lê o estado
# JÁ commitado (turns atualizados), processando com estado fresco.
#
# Segue o padrão do Inspector (pipeline.py): lock de sessão preso a uma conexão
# dedicada, mantida aberta por todo o span de process_incoming (inclui a chamada
# LLM e o envio) e liberada no finally. NÃO uso pg_advisory_xact_lock porque o
# orchestrator abre/fecha múltiplas sessões e faz trabalho lento (LLM/HTTP)
# entre elas — não quero segurar uma transação aberta atravessando isso.
#
# key1 é namespace fixo próprio do recovery (distinto do 0x42455454 do
# Inspector, pra não colidir no espaço de advisory locks de dois inteiros);
# key2 é derivado do lead_id (string) via CRC32 → int32 assinado.
_ADVISORY_LOCK_NAMESPACE = 0x52454356  # "RECV" — namespace do Recovery Acme


def _lead_lock_key(lead_id: str) -> int:
    """Deriva um int32 assinado estável a partir do lead_id (string)."""
    unsigned = zlib.crc32(lead_id.encode("utf-8")) & 0xFFFFFFFF
    # advisory lock usa int4 assinado; converte para o range [-2^31, 2^31-1].
    return unsigned - 0x100000000 if unsigned >= 0x80000000 else unsigned


def _acquire_lead_lock(lead_id: str) -> Any:
    """Adquire advisory lock de sessão pro lead (BLOQUEANTE). Retorna a conexão
    que segura a trava — o caller DEVE chamar _release_lead_lock no finally."""
    key = _lead_lock_key(lead_id)
    conn = engine.connect()
    try:
        conn.execute(
            text("SELECT pg_advisory_lock(:ns, :k)"),
            {"ns": _ADVISORY_LOCK_NAMESPACE, "k": key},
        )
    except Exception:
        conn.close()
        raise
    return conn


def _release_lead_lock(conn: Any, lead_id: str) -> None:
    """Libera o advisory lock e fecha a conexão. Best-effort (não levanta)."""
    if conn is None:
        return
    key = _lead_lock_key(lead_id)
    try:
        conn.execute(
            text("SELECT pg_advisory_unlock(:ns, :k)"),
            {"ns": _ADVISORY_LOCK_NAMESPACE, "k": key},
        )
    except Exception:
        log.exception("recovery_advisory_unlock_failed", lead_id=lead_id)
    finally:
        try:
            conn.close()
        except Exception:
            log.exception("recovery_advisory_conn_close_failed", lead_id=lead_id)


def process_incoming(
    *,
    lead_phone: str,
    lead_name: str | None,
    lead_id: str | None,
    text: str,
) -> ProcessResult:
    """Wrapper público: serializa por lead (advisory lock) e delega o pipeline.

    Duas mensagens distintas do mesmo lead quase simultâneas são processadas em
    série — a 2ª espera a 1ª e lê o estado já commitado (Codex F5).
    """
    if not lead_id:
        log.info("recovery_skip_no_lead_id", phone_last4=lead_phone[-4:])
        return ProcessResult(sent=False, skipped_reason="no_lead_id")

    lock_conn = _acquire_lead_lock(lead_id)
    try:
        return _process_incoming_locked(
            lead_phone=lead_phone,
            lead_name=lead_name,
            lead_id=lead_id,
            text=text,
        )
    finally:
        _release_lead_lock(lock_conn, lead_id)


def _process_incoming_locked(
    *,
    lead_phone: str,
    lead_name: str | None,
    lead_id: str,
    text: str,
) -> ProcessResult:
    """
    Pipeline completo de uma msg recebida do lead.

    Args:
        lead_phone: telefone normalizado (dígitos).
        lead_name: nome do lead (do Kommo ou Evolution).
        lead_id: ID Kommo do lead (None = lead não rastreado no CRM, ignora).
        text: corpo da mensagem.

    Pré-condição: lead_id != None e o advisory lock por lead JÁ foi adquirido
    pelo wrapper público `process_incoming` (serialização Codex F5).
    """
    with SessionLocal() as session:
        conv_row = repo.get_or_create(
            session, lead_id=lead_id, lead_phone=lead_phone, lead_name=lead_name
        )

        if is_terminal(State(conv_row.state)):
            log.info(
                "recovery_skip_terminal_state",
                lead_id=lead_id, state=conv_row.state,
            )
            session.commit()
            return ProcessResult(sent=False, skipped_reason=f"terminal:{conv_row.state}")

        conv = repo.to_domain(conv_row)
        now = datetime.now(timezone.utc)
        conv.on_lead_message(now)
        repo.append_message(session, lead_id, role="lead", text=text)
        repo.save_state(session, conv_row, conv)
        session.commit()

        history = repo.load_history(session, lead_id)

    try:
        reply = generate_reply(lead_name, history)
    except Exception as exc:
        log.error("recovery_responder_failed", lead_id=lead_id, error=str(exc))
        return ProcessResult(sent=False, skipped_reason="responder_error")

    # Quando a Corretor decide encerrar sem responder (ex: lead é outra corretora,
    # auto-reply de ausência) o LLM volta só com o bloco <state> e text vazio.
    # Evolution rejeita texto vazio com 400 — pula o send mas mantém o resto
    # do fluxo (state update + finalize Kommo).
    if reply.text and reply.text.strip():
        sent = _send_via_evolution(lead_phone, reply.text)
        if not sent:
            # HAVIA texto pra entregar e o envio real (Evolution) falhou: NÃO
            # persistir a mensagem nem transicionar/finalizar no Kommo — senão
            # o histórico afirma uma mensagem que o lead nunca recebeu
            # ("conversa fantasma", Codex #6). O encerramento intencional
            # (reply.text vazio) continua seguindo o fluxo normal abaixo.
            log.error("recovery_send_failed_abort_persist", lead_id=lead_id)
            return ProcessResult(sent=False, skipped_reason="send_failed")
    else:
        log.info("recovery_skip_empty_reply", lead_id=lead_id,
                 ended=reply.ended, reason=reply.end_reason)
        sent = False

    with SessionLocal() as session:
        conv_row = repo.get_or_create(
            session, lead_id=lead_id, lead_phone=lead_phone, lead_name=lead_name
        )
        conv = repo.to_domain(conv_row)
        now = datetime.now(timezone.utc)
        conv.on_corretor_sent(
            now, qualified=reply.qualified, ended=reply.ended, reason=reply.end_reason
        )
        repo.append_message(session, lead_id, role="corretor", text=reply.text)
        repo.save_state(session, conv_row, conv)
        session.commit()

        if conv.state in (State.QUALIFICADO, State.PERDIDO):
            _finalize_in_kommo(session, conv_row, history + [
                # adiciona a última msg da Corretor ao histórico passado pro extractor
                # (já foi gravada no DB acima, mas evita SELECT novo)
            ])
            session.commit()

    return ProcessResult(
        sent=sent, reply_text=reply.text, state=conv.state.value
    )


def _send_via_evolution(lead_phone: str, text: str) -> bool:
    if settings.dry_run:
        log.info(
            "recovery_dry_run_send",
            phone_last4=lead_phone[-4:], chars=len(text),
        )
        return False
    try:
        client = EvolutionClient()
        client.send_text(
            instance_name=settings.evolution_recuperador_instance,
            to_number=lead_phone,
            text=text,
        )
        return True
    except Exception as exc:
        log.error("recovery_send_failed", phone_last4=lead_phone[-4:], error=str(exc))
        return False


def _finalize_in_kommo(
    session: Session, conv_row, history_for_extractor
) -> None:
    """Roda extractor, grava nota Kommo e move pra etapa Respondida/Perdida."""
    target_stage = (
        settings.kommo_stage_remarketing_respondida
        if conv_row.state == State.QUALIFICADO.value
        else settings.kommo_stage_remarketing_perdida
    )

    if not target_stage:
        log.warning(
            "recovery_kommo_skip_no_stage_configured",
            lead_id=conv_row.lead_id, state=conv_row.state,
        )
        return

    full_history = repo.load_history(session, conv_row.lead_id)

    try:
        # Pina o modelo: sem isso o CLI herda o default da conta da Dona
        # (~/.claude/settings.json), que muda quando ela troca de modelo.
        provider = ClaudeCliProvider(timeout=60, model=settings.model_analyzer)
        result: QualificationResult = extract(
            provider, conv_row.lead_name, full_history
        )
    except Exception as exc:
        log.error("recovery_extractor_failed", lead_id=conv_row.lead_id, error=str(exc))
        return

    conv_row.qualified_data = result.model_dump()
    session.flush()

    kommo = KommoTool()
    try:
        kommo.add_lead_note(conv_row.lead_id, result.resumo_nota)
    except Exception as exc:
        log.error("recovery_kommo_note_failed", lead_id=conv_row.lead_id, error=str(exc))

    try:
        kommo.move_lead_stage(
            conv_row.lead_id,
            int(target_stage),
            pipeline_id=settings.kommo_pipeline_id_remarketing or None,
        )
    except Exception as exc:
        log.error("recovery_kommo_move_failed", lead_id=conv_row.lead_id, error=str(exc))
