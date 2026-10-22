"""Trava anti-colisão entre os dois motores de reengajamento (F1 do B9).

Dois motores tocam lead frio por WhatsApp, cada um com estado próprio e SEM
dedup entre si:

  - **Corretor/recovery** (linha Acme 0000): `app/recovery/dispatcher.py`, estado
    em `recovery_conversations` (aguardando/qualificando/reengajado/
    qualificado/perdido/largado);
  - **Cadência 8 toques + nutrição** (linha Pessoa12 2591): `app/services/cadencia.py`
    e `app/services/nutricao.py`, estado em `cadencia_estado`
    (ativa/remarketing/respondida/opt_out).

O mesmo telefone pode estar vivo nos dois → lead recebe mensagem das duas
linhas no mesmo dia. Este módulo dá duas consultas puras (por telefone,
match pelos últimos 8 dígitos — padrão p8 do projeto) pros 4 pontos de envio
checarem o OUTRO motor antes de disparar:

  - `corretor_conversa_ativa(tel)`  → cadência/nutrição/auto-enroll consultam a Corretor;
  - `cadencia_engajada(tel)`   → dispatcher da Corretor consulta a cadência;
  - `ligacao_recente(tel)`     → cadência/nutrição consultam a JANELA DE LIGAÇÃO
    (05/08). Entrou um TERCEIRO motor que toca o mesmo lead frio, e ele não é
    WhatsApp: a corretora liga. O funil `remarketing` guarda os dois mundos —
    as etapas 30/60/90/120/150 dias (régua de nutrição, linha da Pessoa12) e a roda do
    carrossel — e a fila de ligação puxa justamente os leads de nutrição, que
    são os sem dono. Com 548 leads descongelados em 05/08 (~30/dia voltando a
    receber marco), a mesma pessoa levaria ligação DE MANHÃ e mensagem
    automática NO MESMO DIA, de duas fontes que não se falam.
  - `automacao_recente(tel)`   → a janela de ligação consulta os motores de
    WhatsApp (é o outro lado da mesma trava).

REGRA DE OURO: a trava só IMPEDE envio duplicado — nunca envia nada e, em
erro/dúvida na query, **NÃO bloqueia** (fail-open pro fluxo normal, pra um bug
da trava não parar a cadência). Todo erro é logado
(`reengajamento_guard_erro`); todo bloqueio é logado no ponto de aplicação
(`reengajamento_guard_bloqueou` motor=... telefone_last4=...).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Callable

import structlog
from sqlalchemy.orm import Session

from app.db.models import CadenciaEstado, RecoveryConversation

log = structlog.get_logger()

# Estados em que a Corretor ainda está (ou pode voltar a) conversar com o lead.
# Finais (qualificado/perdido/largado) liberam o telefone.
CORRETOR_STATES_ATIVOS: tuple[str, ...] = ("aguardando", "qualificando", "reengajado")

# Janela de atividade — mesma janela de 7 dias do recovery_dash
# (`hsm_sent_at > now() - interval '7 days'`). Além do hsm_sent_at, considero
# `last_lead_msg_at` recente: lead que ainda está respondendo à Corretor depois do
# 7º dia do HSM continua em conversa ativa (o dash só não o lista).
CORRETOR_JANELA_DIAS = 7

# ── Janela de silêncio ligação × automação (05/08) ───────────────────────────
#
# 3 dias, os DOIS lados com o mesmo número (a trava é simétrica: quem ligou não
# recebe mensagem, quem recebeu mensagem não é oferecido pra ligar).
#
# POR QUE 3 E NÃO 30: a régua de nutrição é 1 mensagem a cada 30 dias, então
# uma janela de 30 dias esvaziaria a fila de ligação inteira — 536 dos 545 leads
# da fila têm toque agendado (medido em 05/08). O que a Dona descreveu como
# atropelo é "ligação + mensagem automática no mesmo dia"; 3 dias mata isso com
# folga e ainda deixa a fila utilizável. Quem levar ligação hoje volta a ser
# elegível pra régua depois de amanhã — não perde o marco, ele ATRASA.
JANELA_SILENCIO_DIAS = 3

DEFAULT_TENANT_ID = 1


def _p8(telefone: str | None) -> str:
    """Últimos 8 dígitos — mesma heurística de match do projeto
    (`cadencia._suf8`; casa '+5515...', '5515...', wa_id sem o 9, etc.).
    Reimplementada aqui pra não importar `cadencia` (evita ciclo — a cadência
    importa este módulo)."""
    return re.sub(r"\D", "", telefone or "")[-8:]


def _as_utc(ts: datetime | None) -> datetime | None:
    if ts is not None and ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)  # SQLite de teste devolve naive
    return ts


def _run_query(
    fn: Callable[[Session], bool],
    *,
    nome: str,
    telefone_last4: str,
    session: Session | None,
    session_factory: Callable[[], Session] | None,
) -> bool:
    """Executa a consulta com fail-open. Sessão do caller roda em SAVEPOINT
    (`begin_nested`) — erro na trava (ex.: tabela ausente num DB de teste
    enxuto) não pode poisonar a transação de quem chamou."""
    try:
        if session is not None:
            with session.begin_nested():
                return fn(session)
        if session_factory is None:
            from app.db.session import SessionLocal

            session_factory = SessionLocal
        with session_factory() as s:
            return fn(s)
    except Exception as e:  # noqa: BLE001 — fail-open: trava nunca para o motor
        log.warning(
            "reengajamento_guard_erro",
            func=nome,
            telefone_last4=telefone_last4,
            erro=str(e)[:160],
        )
        return False


def corretor_conversa_ativa(
    telefone: str | None,
    *,
    agora: datetime | None = None,
    session: Session | None = None,
    session_factory: Callable[[], Session] | None = None,
) -> bool:
    """True se o telefone tem conversa VIVA com a Corretor (recovery):
    `recovery_conversations` com state em CORRETOR_STATES_ATIVOS e atividade
    (`hsm_sent_at` OU `last_lead_msg_at`) nos últimos CORRETOR_JANELA_DIAS dias.

    Fail-open: erro na consulta → False (+ log `reengajamento_guard_erro`).
    """
    suf = _p8(telefone)
    if not suf:
        return False
    agora = agora or datetime.now(timezone.utc)
    cutoff = agora - timedelta(days=CORRETOR_JANELA_DIAS)

    def _q(s: Session) -> bool:
        rows = (
            s.query(
                RecoveryConversation.lead_phone,
                RecoveryConversation.hsm_sent_at,
                RecoveryConversation.last_lead_msg_at,
            )
            .filter(RecoveryConversation.state.in_(CORRETOR_STATES_ATIVOS))
            .all()
        )
        for phone, hsm_at, lead_at in rows:
            if _p8(phone) != suf:
                continue
            for ts in (_as_utc(hsm_at), _as_utc(lead_at)):
                if ts is not None and ts >= cutoff:
                    return True
        return False

    return _run_query(
        _q,
        nome="corretor_conversa_ativa",
        telefone_last4=suf[-4:],
        session=session,
        session_factory=session_factory,
    )


def cadencia_engajada(
    telefone: str | None,
    *,
    tenant_id: int = DEFAULT_TENANT_ID,
    session: Session | None = None,
    session_factory: Callable[[], Session] | None = None,
) -> bool:
    """True se o telefone está ATIVO na cadência de 8 toques
    (`cadencia_estado.status == 'ativa'`).

    'remarketing' NÃO bloqueia: `cadencia_estado` não tem campo de resposta —
    quando o lead responde o status vira 'respondida'/'opt_out'
    (`processar_resposta_cadencia`), logo remarketing é, por definição, lead
    frio há 30d+ sem resposta (não é conversa em andamento).

    Fail-open: erro na consulta → False (+ log `reengajamento_guard_erro`).
    """
    suf = _p8(telefone)
    if not suf:
        return False

    def _q(s: Session) -> bool:
        rows = (
            s.query(CadenciaEstado.telefone)
            .filter(
                CadenciaEstado.tenant_id == tenant_id,
                CadenciaEstado.status == "ativa",
            )
            .all()
        )
        return any(_p8(r[0]) == suf for r in rows)

    return _run_query(
        _q,
        nome="cadencia_engajada",
        telefone_last4=suf[-4:],
        session=session,
        session_factory=session_factory,
    )


def ligacao_recente(
    telefone: str | None,
    *,
    dias: int = JANELA_SILENCIO_DIAS,
    tenant_id: int = DEFAULT_TENANT_ID,
    agora: datetime | None = None,
    session: Session | None = None,
    session_factory: Callable[[], Session] | None = None,
) -> bool:
    """True se ALGUMA corretora ligou pra este telefone nos últimos `dias`.

    É a trava que a cadência e a nutrição consultam antes de mandar mensagem
    automática: contato humano por VOZ acabou de acontecer, e a régua entrando
    por cima no mesmo dia é o "vocês não se conversam aí?".

    Vale pra QUALQUER resultado — inclusive "não atendeu". A ligação não
    atendida deixa chamada perdida no celular da pessoa; mensagem automática
    logo atrás é a mesma sobreposição.

    Casamento telefone → ligação: `ligacao_resultado` guarda a chave do lead
    (external_ref ou leads.id na origem 'remarketing'), não o telefone — então
    passa por `leads`, com o sufixo-8 de sempre.

    Fail-open: erro na consulta → False (+ log `reengajamento_guard_erro`).
    """
    suf = _p8(telefone)
    if not suf:
        return False
    agora = agora or datetime.now(timezone.utc)
    cutoff = agora - timedelta(days=dias)

    def _q(s: Session) -> bool:
        from sqlalchemy import text as _sql

        return bool(
            s.execute(
                _sql(
                    r"""
                    SELECT 1
                      FROM ligacao_resultado lr
                      JOIN leads l ON l.tenant_id = lr.tenant_id
                       AND ((lr.origem = 'remarketing' AND lr.origem_id = l.id)
                            OR (l.external_ref IS NOT NULL
                                AND lr.lead_id = l.external_ref))
                     WHERE lr.tenant_id = :t
                       AND lr.criado_em >= :cutoff
                       AND right(regexp_replace(
                             COALESCE(l.telefone, ''), '\D', '', 'g'), 8) = :suf
                     LIMIT 1
                    """
                ),
                {"t": tenant_id, "cutoff": cutoff, "suf": suf},
            ).first()
        )

    return _run_query(
        _q,
        nome="ligacao_recente",
        telefone_last4=suf[-4:],
        session=session,
        session_factory=session_factory,
    )


# ── Conversa HUMANA em andamento (06/08) ─────────────────────────────────────
#
# As travas acima protegem os robôs uns dos outros. Faltava a mais óbvia: a
# CORRETORA está conversando com a pessoa AGORA, na mão.
#
# Achado 06/08 na linha da Pessoa11: de 37 reengajamentos disparados em 8h, 22 (59%)
# foram pra quem trocou mensagem de verdade com ela na última semana — um deles
# com 70 mensagens no período, e um que recebeu o template às 16:54 estando em
# conversa às 16:58. Ela teve que escrever "desconsidera essa última mensagem"
# pra uma cliente. Reengajar quem já está engajado não é só inútil: desmente a
# corretora na frente do cliente.
#
# `cadencia_estado.status` deveria virar 'respondida' e parar a régua, mas isso
# depende da resposta cair na janela/linha que o motor observa. Esta trava não
# depende de estado nenhum: olha a conversa real.
JANELA_CONVERSA_HUMANA_DIAS = 7

# Tipos que NÃO contam como conversa humana: são os próprios disparos e os
# eventos de sistema. Sem isso a trava se auto-alimentaria (o template de ontem
# bloquearia o de hoje) e nunca mais sairia reengajamento nenhum.
_TIPOS_NAO_HUMANOS: tuple[str, ...] = ("template", "revoke", "reaction", "edit")


def conversa_humana_ativa(
    telefone: str | None,
    *,
    dias: int = JANELA_CONVERSA_HUMANA_DIAS,
    tenant_id: int = DEFAULT_TENANT_ID,
    agora: datetime | None = None,
    session: Session | None = None,
    session_factory: Callable[[], Session] | None = None,
) -> bool:
    """True se há conversa DE VERDADE com este telefone nos últimos `dias`.

    Conta mensagem real em qualquer direção (texto, áudio, imagem, documento…),
    da corretora ou do lead. NÃO conta template nem evento de sistema — senão o
    próprio disparo manteria a trava viva pra sempre.

    Quem está em conversa não é lead frio: não recebe reengajamento.

    Fail-open: erro na consulta → False (+ log `reengajamento_guard_erro`).
    """
    suf = _p8(telefone)
    if not suf:
        return False
    agora = agora or datetime.now(timezone.utc)
    cutoff = agora - timedelta(days=dias)

    def _q(s: Session) -> bool:
        from sqlalchemy import text as _sql

        return bool(
            s.execute(
                _sql(
                    r"""
                    SELECT 1
                      FROM mensagens_whatsapp me
                     WHERE me.timestamp_evolution >= :cutoff
                       AND right(regexp_replace(
                             COALESCE(me.contato_telefone, ''), '\D', '', 'g'), 8) = :suf
                       AND COALESCE(me.tipo_mensagem, '') <> ALL(:nao_humanos)
                     LIMIT 1
                    """
                ),
                {"cutoff": cutoff, "suf": suf,
                 "nao_humanos": list(_TIPOS_NAO_HUMANOS)},
            ).first()
        )

    return _run_query(
        _q,
        nome="conversa_humana_ativa",
        telefone_last4=suf[-4:],
        session=session,
        session_factory=session_factory,
    )


def automacao_recente(
    telefone: str | None,
    *,
    dias: int = JANELA_SILENCIO_DIAS,
    tenant_id: int = DEFAULT_TENANT_ID,
    agora: datetime | None = None,
    session: Session | None = None,
    session_factory: Callable[[], Session] | None = None,
) -> bool:
    """True se algum motor automático mandou mensagem pra este telefone nos
    últimos `dias` — o OUTRO lado da trava, consultado pela fila de ligação.

    Duas fontes, que são os dois motores que escrevem pro lead frio:
      - `cadencia_eventos(tipo='mensagem')` — cadência de 8 toques E nutrição
        (a nutrição grava no mesmo log, com `payload.nutricao = true`);
      - `lead_events(tipo='carrossel_disparo')` — carrossel da Corretor.

    Fail-open: erro na consulta → False (+ log `reengajamento_guard_erro`).
    """
    suf = _p8(telefone)
    if not suf:
        return False
    agora = agora or datetime.now(timezone.utc)
    cutoff = agora - timedelta(days=dias)

    def _q(s: Session) -> bool:
        from sqlalchemy import text as _sql

        return bool(
            s.execute(
                _sql(
                    r"""
                    SELECT 1 FROM cadencia_eventos ev
                      JOIN cadencia_estado ce ON ce.id = ev.cadencia_id
                     WHERE ev.tenant_id = :t AND ev.tipo = 'mensagem'
                       AND ev.at >= :cutoff
                       AND right(regexp_replace(
                             COALESCE(ce.telefone, ''), '\D', '', 'g'), 8) = :suf
                     LIMIT 1
                    """
                ),
                {"t": tenant_id, "cutoff": cutoff, "suf": suf},
            ).first()
        ) or bool(
            s.execute(
                _sql(
                    r"""
                    SELECT 1 FROM lead_events le
                      JOIN leads l ON l.id = le.lead_id
                     WHERE le.tenant_id = :t AND le.tipo = 'carrossel_disparo'
                       AND le.at >= :cutoff
                       AND right(regexp_replace(
                             COALESCE(l.telefone, ''), '\D', '', 'g'), 8) = :suf
                     LIMIT 1
                    """
                ),
                {"t": tenant_id, "cutoff": cutoff, "suf": suf},
            ).first()
        )

    return _run_query(
        _q,
        nome="automacao_recente",
        telefone_last4=suf[-4:],
        session=session,
        session_factory=session_factory,
    )
