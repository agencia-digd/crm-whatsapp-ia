"""Lembrete de pendência da Assistente — registro incompleto de visita/venda.

Motivação (incidente 22/07/2026): a Pessoa10 contou pra Assistente que o Guilherme
"visitou e fechou a venda", a Assistente pediu 2 infos (hora da visita + valor da
venda) e a Pessoa10 nunca respondeu. O fluxo morreu em silêncio e uma venda de
R$ 423k ficou 2 dias fora do painel.

Este módulo varre `conversations` por corretora e detecta threads onde:
  - a ÚLTIMA mensagem é da Assistente (a corretora não mandou NADA depois);
  - alguma mensagem da cauda de mensagens consecutivas da assistente é uma
    pergunta de REGISTRO (heurística: tem "?" e menciona registrar/visita/
    venda/que horas/valor);
  - a pendência tem mais de 18h e menos de 48h (cobra UMA vez, no dia seguinte).

Dedup: `assistente_lembretes_enviados` guarda (corretora_id, conversa_ultima_msg_at)
com UNIQUE — a mesma pendência nunca é cobrada 2x. O marcador só é gravado em
envio REAL (dry-run apenas loga, pra rodada de teste não "queimar" a cobrança).

Travas de envio (fail-closed, TODAS precisam liberar):
  - `settings.lembrete_pendencia_ativo` (env LEMBRETE_PENDENCIA_ATIVO, default False);
  - `settings.dry_run` False;
  - fora de pytest (`PYTEST_CURRENT_TEST`).
Sem isso o run só LOGA o que enviaria.

Tom da mensagem: gentil, nunca cobrança — corretora não pode se sentir vigiada
(regra da casa, mesma matriz do briefing matinal).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select

from app.config import settings
from app.db.models import AssistenteLembreteEnviado, Conversation, Corretora
from app.db.session import SessionLocal
from app.services.phone import normalize_phone

log = structlog.get_logger()

MIN_HOURS_DEFAULT = 18.0
MAX_HOURS_DEFAULT = 48.0

# Heurística de "pergunta de registro": a mensagem pergunta algo E fala de
# registrar visita/venda (ou pede os dados que faltam pro registro).
_KEYWORDS_REGISTRO = ("registrar", "visita", "venda", "que horas", "valor")


@dataclass
class Pendencia:
    """Uma pendência de registro detectada numa thread Assistente↔corretora."""

    corretora_id: int
    corretora_nome: str
    phone: str  # E.164 sem '+' (formato de conversations.phone)
    ultima_msg_at: datetime  # created_at da ÚLTIMA msg da thread (chave do dedup)
    pergunta: str  # texto da pergunta de registro que ficou sem resposta


def eh_pergunta_registro(texto: str) -> bool:
    """True se o texto é uma pergunta de registro (heurística barata, sem LLM)."""
    t = texto.lower()
    return "?" in t and any(kw in t for kw in _KEYWORDS_REGISTRO)


def detectar_pendencia_thread(
    mensagens: list[tuple[str, str, datetime]],
    agora: datetime,
    min_hours: float = MIN_HOURS_DEFAULT,
    max_hours: float = MAX_HOURS_DEFAULT,
) -> tuple[str, datetime] | None:
    """
    Detecta pendência numa thread. Função PURA (testável sem banco).

    Args:
        mensagens: lista de (message_role, text, created_at) em ordem ASC.
        agora: instante de referência (tz-aware).
        min_hours/max_hours: janela de idade da pendência.

    Returns:
        (pergunta, ultima_msg_at) se há pendência, senão None.

    A Assistente costuma mandar várias mensagens seguidas — por isso a pergunta é
    procurada em TODA a cauda de mensagens consecutivas da assistente (no
    incidente da Pessoa10, a pergunta das 2 infos era a penúltima msg da cauda).
    """
    if not mensagens:
        return None

    # Cauda: mensagens consecutivas da assistente no fim da thread.
    cauda: list[tuple[str, datetime]] = []
    for role, text, created_at in reversed(mensagens):
        if role != "assistant":
            break
        cauda.append((text, created_at))
    if not cauda:
        return None  # última msg é da corretora → ela respondeu, sem pendência

    ultima_msg_at = cauda[0][1]
    idade = agora - ultima_msg_at
    if not (timedelta(hours=min_hours) <= idade <= timedelta(hours=max_hours)):
        return None

    # Varre em ordem CRONOLÓGICA (cauda está do mais novo pro mais velho):
    # a pergunta original — geralmente a mais completa, ex: "que horas + valor"
    # no incidente da Pessoa10 — vence sobre follow-ups posteriores da Assistente.
    for text, _ in reversed(cauda):
        if eh_pergunta_registro(text):
            return text, ultima_msg_at
    return None


def montar_mensagem(nome_corretora: str, pergunta: str) -> str:
    """Monta o lembrete gentil a partir da pergunta que ficou sem resposta."""
    primeiro_nome = nome_corretora.strip().split()[0]
    q = pergunta.lower()

    faltando: list[str] = []
    if any(kw in q for kw in ("que horas", "horário", "horario", "hora")):
        faltando.append("o horário da visita")
    if "valor" in q:
        faltando.append("o valor da venda")
    falta_txt = " e ".join(faltando) if faltando else "algumas informações"

    if "venda" in q and "visita" in q:
        resumo = "uma visita e uma venda"
    elif "venda" in q:
        resumo = "uma venda"
    elif "visita" in q:
        resumo = "uma visita"
    else:
        resumo = "um registro"

    return (
        f"Oi {primeiro_nome}! Ontem você me contou sobre {resumo}, mas ficou "
        f"faltando {falta_txt} pra eu deixar tudo registrado certinho. "
        f"Me manda quando puder que eu cuido do resto 😊"
    )


def _envio_real_liberado() -> bool:
    """Fail-closed: só envia com flag própria ON + DRY_RUN off + fora de pytest."""
    return (
        settings.lembrete_pendencia_ativo
        and not settings.dry_run
        and not os.environ.get("PYTEST_CURRENT_TEST")
    )


def run(
    agora: datetime | None = None,
    min_hours: float = MIN_HOURS_DEFAULT,
    max_hours: float = MAX_HOURS_DEFAULT,
) -> list[Pendencia]:
    """
    Varre as corretoras ativas, detecta pendências e envia (ou loga) o lembrete.

    Returns:
        Lista de pendências detectadas (já deduplicadas).
    """
    agora = agora or datetime.now(timezone.utc)
    lookback = agora - timedelta(hours=max_hours)
    detectadas: list[Pendencia] = []

    with SessionLocal() as s:
        corretoras = (
            s.execute(
                select(Corretora).where(
                    Corretora.ativo.is_(True), Corretora.tenant_id == 1
                )
            )
            .scalars()
            .all()
        )

        for c in corretoras:
            try:
                phone = normalize_phone(c.telefone)
            except ValueError:
                log.warning("lembrete_pendencia_phone_invalido", corretora_id=c.id)
                continue

            rows = s.execute(
                select(
                    Conversation.message_role,
                    Conversation.text,
                    Conversation.created_at,
                )
                .where(
                    Conversation.phone == phone,
                    Conversation.created_at >= lookback,
                )
                .order_by(Conversation.created_at.asc())
            ).all()

            resultado = detectar_pendencia_thread(
                [(r[0], r[1], r[2]) for r in rows], agora, min_hours, max_hours
            )
            if resultado is None:
                continue
            pergunta, ultima_msg_at = resultado

            # Dedup: já cobramos essa pendência?
            ja_enviado = s.execute(
                select(AssistenteLembreteEnviado.id).where(
                    AssistenteLembreteEnviado.corretora_id == c.id,
                    AssistenteLembreteEnviado.conversa_ultima_msg_at == ultima_msg_at,
                )
            ).first()
            if ja_enviado:
                log.info(
                    "lembrete_pendencia_ja_cobrado",
                    corretora=c.nome,
                    ultima_msg_at=ultima_msg_at.isoformat(),
                )
                continue

            pendencia = Pendencia(
                corretora_id=c.id,
                corretora_nome=c.nome,
                phone=phone,
                ultima_msg_at=ultima_msg_at,
                pergunta=pergunta,
            )
            detectadas.append(pendencia)
            mensagem = montar_mensagem(c.nome, pergunta)

            if not _envio_real_liberado():
                # Dry-run: loga a mensagem composta (não loga conteúdo da conversa)
                # e NÃO grava marcador — rodada de teste não queima a cobrança.
                log.info(
                    "lembrete_pendencia_dry_run",
                    corretora=c.nome,
                    phone_last4=phone[-4:],
                    ultima_msg_at=ultima_msg_at.isoformat(),
                    idade_horas=round((agora - ultima_msg_at).total_seconds() / 3600, 1),
                    mensagem=mensagem,
                    motivo_dry="flag LEMBRETE_PENDENCIA_ATIVO off"
                    if not settings.lembrete_pendencia_ativo
                    else "DRY_RUN global on",
                )
                continue

            from app.integrations.evolution_client import EvolutionClient

            try:
                cli = EvolutionClient()
                r = cli.send_text(settings.evolution_assistente_instance, phone, mensagem)
                msg_id = (r.get("key") or {}).get("id", "?")
                s.add(
                    AssistenteLembreteEnviado(
                        corretora_id=c.id, conversa_ultima_msg_at=ultima_msg_at
                    )
                )
                s.commit()
                log.info(
                    "lembrete_pendencia_enviado",
                    corretora=c.nome,
                    phone_last4=phone[-4:],
                    msg_id=str(msg_id)[:24],
                )
            except Exception as e:
                log.error(
                    "lembrete_pendencia_envio_falhou",
                    corretora=c.nome,
                    erro=str(e)[:150],
                )

    log.info("lembrete_pendencia_run_done", pendencias=len(detectadas))
    return detectadas
