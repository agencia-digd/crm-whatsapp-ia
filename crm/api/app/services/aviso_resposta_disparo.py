"""Avisa a Dona, no Telegram do Copiloto, cada lead que respondeu o disparo.

Pedido da Dona em 06/09/2026: ela gostou do digest cru das conversas da Pessoa
(`scripts/relatorio_pessoa_primeiras.py`) e pediu pra receber SEMPRE — não uma
vez por dia, não só as 3 primeiras. Formato igual: texto do lead e da Pessoa,
sem resumo de IA.

Refino no mesmo dia: não mandar no primeiro "oi". Esperar 5 mensagens
DO LEAD (`MIN_MSGS`) pra ela acompanhar o que está acontecendo — se
avisar cedo demais, a marca do dia trava e o restante some. Canal: bot
do Copiloto (`TelegramTool.send_to_dona`), não o chat da Assistente.

O que conta como "respondeu o disparo": conversa de HOJE (BRT) na tabela
`conversations` em que a Pessoa falou — é o rastro do carrossel da Corretor. Cadência
das corretoras humanas já cai no WhatsApp/inbox delas; aqui é o robô, que a
Dona quer ler.

Idempotente por telefone+dia: o mesmo lead não vira 2 avisos no mesmo dia.
Best-effort: nunca levanta (webhook e tick da Pessoa não podem cair por causa
de Telegram). Sob pytest/DRY_RUN não envia.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text as sql_text

from app.config import settings
from app.db.session import SessionLocal

log = structlog.get_logger()

TZ = ZoneInfo("America/Sao_Paulo")
ESTADO_PADRAO = Path("/var/lib/copiloto/aviso-resposta-disparo.json")
MAX_MSGS = 20
MAX_CHARS_MSG = 800
MAX_TELEGRAM = 3900
# Dona 06/09: "depois que o lead trocou cinco mensagens". Conta fala
# do lead, não o total da thread (a Pessoa no meio não adianta o gatilho).
# Abaixo disso o próximo inbound/tick tenta de novo (não marca).
MIN_MSGS = 5

_SQL_HOJE = """
    SELECT phone, message_role, sender_kind, text, created_at
      FROM conversations
     WHERE (created_at AT TIME ZONE 'America/Sao_Paulo')::date
         = (now() AT TIME ZONE 'America/Sao_Paulo')::date
       AND phone IN (
            SELECT DISTINCT phone FROM conversations
             WHERE sender_kind = 'pessoa'
               AND (created_at AT TIME ZONE 'America/Sao_Paulo')::date
                 = (now() AT TIME ZONE 'America/Sao_Paulo')::date
       )
     ORDER BY phone, created_at
"""

_SQL_DISPAROS = """
    SELECT count(*) FROM lead_events
     WHERE tipo = 'carrossel_disparo'
       AND (at AT TIME ZONE 'America/Sao_Paulo')::date
         = (now() AT TIME ZONE 'America/Sao_Paulo')::date
"""

# Testes e o script apontam pra cá.
ESTADO: Path = ESTADO_PADRAO


def _hoje() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def _chave(phone: str, dia: str | None = None) -> str:
    return f"{dia or _hoje()}:{phone}"


def _ler_estado() -> set[str]:
    try:
        data = json.loads(ESTADO.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return set(str(x) for x in data)
        if isinstance(data, dict):
            return set(str(k) for k in data)
    except (OSError, ValueError):
        return set()
    return set()


def _gravar_estado(chaves: set[str]) -> None:
    try:
        ESTADO.parent.mkdir(parents=True, exist_ok=True)
        ESTADO.write_text(
            json.dumps(sorted(chaves), ensure_ascii=False), encoding="utf-8"
        )
    except OSError as e:
        log.warning("aviso_resposta_estado_falhou", erro=str(e)[:120])


def _pronta(msgs: list[Any] | None) -> bool:
    """Tem 5 falas do lead — a Dona acompanha o que está acontecendo."""
    if not msgs:
        return False
    do_lead = sum(1 for m in msgs if (m["sender_kind"] or "") != "pessoa")
    return do_lead >= MIN_MSGS


def _disparos_hoje(s) -> int:
    return int(s.execute(sql_text(_SQL_DISPAROS)).scalar() or 0)


def _conversas_hoje() -> tuple[dict[str, list[Any]], int]:
    with SessionLocal() as s:
        linhas = s.execute(sql_text(_SQL_HOJE)).mappings().all()
        disparos = _disparos_hoje(s)
    por: dict[str, list[Any]] = {}
    for r in linhas:
        por.setdefault(r["phone"], []).append(r)
    return por, disparos


def _txt(valor: str | None) -> str:
    t = (valor or "").strip() or "(sem texto — mídia/áudio)"
    if len(t) > MAX_CHARS_MSG:
        return t[:MAX_CHARS_MSG] + "…"
    return t


def montar_um(fone: str, msgs: list[Any], *, disparos: int | None = None) -> str:
    """O bloco que a Dona gostou — um lead, conversa crua."""
    agora = datetime.now(TZ).strftime("%d/%m %H:%M")
    cab = [f"🐱 *Lead respondeu o disparo* — {agora}", ""]
    if disparos is not None:
        cab.append(f"📤 disparos hoje: *{disparos}*")
    cab.append(f"━━━━━━━━━━\n📱 ...{fone[-4:]}")
    partes = ["\n".join(cab)]
    for m in msgs[:MAX_MSGS]:
        criado = m["created_at"]
        try:
            hora = criado.astimezone(TZ).strftime("%H:%M")
        except Exception:  # noqa: BLE001 — timestamp sem tz não derruba o aviso
            hora = str(criado)[11:16] if criado else "??"
        quem = "👩 Pessoa" if m["sender_kind"] == "pessoa" else "🧑 Lead"
        partes.append(f"\n{quem} ({hora}):\n{_txt(m['text'])}")
    texto = "\n".join(partes)
    if len(texto) > MAX_TELEGRAM:
        texto = texto[:MAX_TELEGRAM] + "\n…"
    return texto


def _enviar(texto: str) -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST") or settings.dry_run:
        log.info("[DRY_RUN] aviso_resposta_disparo", preview=texto[:120])
        return True
    try:
        from app.tools.telegram import TelegramTool

        r = TelegramTool().send_to_dona(texto)
        return str(r).startswith("ok")
    except Exception as e:  # noqa: BLE001
        log.warning("aviso_resposta_telegram_falhou", erro=str(e)[:160])
        return False


def avisar(
    telefone: str,
    *,
    enviar: Callable[[str], bool] | None = None,
) -> bool:
    """Manda a conversa deste telefone se ainda não avisou hoje.

    Devolve True se enviou (ou já tinha avisado). False só se tentou e falhou
    — nesse caso NÃO marca, pra o próximo tick tentar de novo.
    """
    fone = (telefone or "").strip()
    if not fone:
        return False

    por, disparos = _conversas_hoje()
    chave = _chave(fone)
    if chave in _ler_estado():
        return True
    msgs = por.get(fone)
    if not _pronta(msgs):
        return False

    texto = montar_um(fone, msgs, disparos=disparos)
    ok = (enviar or _enviar)(texto)
    if not ok:
        return False
    _gravar_estado(_ler_estado() | {chave})
    log.info("aviso_resposta_disparo_enviado", tel=fone[-4:])
    return True


def avisar_pendentes(
    *,
    enviar: Callable[[str], bool] | None = None,
    forcar: bool = False,
) -> dict[str, int]:
    """Varre as conversas de hoje e avisa as que ainda não saíram."""
    por, disparos = _conversas_hoje()
    if forcar:
        ja = _ler_estado()
        dia = _hoje()
        _gravar_estado({k for k in ja if not k.startswith(f"{dia}:")})

    enviados = 0
    pulados = 0
    falhas = 0
    ja = _ler_estado()
    for fone, msgs in por.items():
        if _chave(fone) in ja and not forcar:
            pulados += 1
            continue
        if not _pronta(msgs):
            pulados += 1
            continue
        texto = montar_um(fone, msgs, disparos=disparos)
        ok = (enviar or _enviar)(texto)
        if ok:
            ja.add(_chave(fone))
            _gravar_estado(ja)
            enviados += 1
        else:
            falhas += 1
    if enviados or falhas:
        log.info(
            "aviso_resposta_pendentes",
            enviados=enviados, pulados=pulados, falhas=falhas,
            conversas=len(por), disparos=disparos,
        )
    return {
        "enviados": enviados,
        "pulados": pulados,
        "falhas": falhas,
        "conversas": len(por),
        "disparos": disparos,
    }


def avisar_em_background(telefone: str) -> None:
    """Hook do inbound: não bloqueia o webhook. Sob pytest é noop."""
    if os.environ.get("COPILOTO_UNDER_PYTEST") or os.environ.get("PYTEST_CURRENT_TEST"):
        return

    def _run() -> None:
        try:
            avisar(telefone)
        except Exception as e:  # noqa: BLE001
            log.warning("aviso_resposta_bg_falhou", erro=str(e)[:160])

    threading.Thread(
        target=_run, daemon=True, name="aviso-resposta-disparo"
    ).start()
