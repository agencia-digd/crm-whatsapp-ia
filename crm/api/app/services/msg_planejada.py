"""Mensagem planejada — a tarefa vira agendador de mensagem (disparo automático).

A corretora anexa uma MENSAGEM PLANEJADA a uma tarefa (`tarefas_corretora`,
migration 0048). Bateu a data/hora → o scheduler manda sozinho, em UTILITY, pela
LINHA DA CORRETOR (mesmo motor da cadência/follow-up, `cloud_corretora_a.enviar_template_corretor`).

Fluxo do tick (rodado por scripts/msg_planejada_tick.py + timer systemd, a cada
5 min no horário comercial SP — espelha o copiloto-cadencia-tick):

  1. Busca tarefas ABERTAS da Pessoa12 (corretora_id=3) com mensagem_planejada não-vazia,
     ainda não enviada (`msg_planejada_enviada_em IS NULL`) e cuja data/hora
     (data_tarefa + COALESCE(hora_tarefa,'09:00') no fuso SP) já venceu.
  2. Pra cada: resolve o TELEFONE do lead (leads_snapshot); pula opt-out;
     respeita o CAP DIÁRIO compartilhado com a cadência (não estoura a linha junto);
     aplica `_limpar_saudacao` (rede de dedupe — o UI já trava a saudação);
     chama `enviar_template_corretor(tel, first_name=None→resolve, hook=corpo)` (UTILITY).
  3. Marca `msg_planejada_enviada_em = now()` (idempotência — 2º tick não reenvia).

Greeting PINADO ao preview do UI: `enviar_template_corretor` com first_name=None resolve
o nome pelo telefone e escolhe DETERMINISTICAMENTE o template certo — COM nome cai
no acme_ola ("Olá {nome}, tudo bem?"), SEM nome cai no acme_oi ("Oi, tudo bem?").
Exatamente o que o UI mostra travado na frente da caixa. Nada de MARKETING: todo o
pool do enviar_template_corretor é UTILITY (regra Dona).

Tudo atrás da trava DRY-RUN global (`settings.dry_run`, default True em dev): em
dry-run loga e NÃO bate na Graph API, mas AINDA marca enviada_em (idempotência
observável no smoke). Best-effort por tarefa: falha em uma não derruba as outras.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import structlog
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.config import settings
from app.services import config_operacional
from app.services.trava_tick import NS_MSG_PLANEJADA, OCUPADO, soltar, travar

log = structlog.get_logger()

# Fase 2 (27/07): deixou de ser só a Pessoa12. A mensagem planejada sai pela linha da
# corretora DONA da tarefa — mandar o template da Pessoa12 pra lead da Pessoa10 faria o
# cliente responder pro número errado, e a corretora perderia a conversa.
CORRETOR_CORRETORA_ID = 3   # mantido: fallback quando a linha não resolve
# Hora default de disparo quando a tarefa não tem hora_tarefa (fuso SP).
HORA_DEFAULT = "09:00"


@dataclass
class MsgPlanejadaResult:
    considerados: int = 0
    enviadas: int = 0
    dry_run: int = 0
    pulados_opt_out: int = 0
    pulados_sem_telefone: int = 0
    bloqueados_cap: int = 0
    falhas: int = 0
    detalhes: list[dict[str, Any]] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Tarefas devidas: Pessoa12, abertas, com mensagem planejada não-vazia, ainda não
# enviada, cuja data/hora (default 09:00 SP) já passou AGORA no fuso SP.
_SQL_DEVIDAS = f"""
    SELECT id, lead_id, lead_nome, mensagem_planejada, corretora_id
    FROM tarefas_corretora
    WHERE status = 'aberta'
      -- CAST obrigatório: sem ele o Postgres não infere o tipo de :cid quando
      -- ele vem NULL (o caso normal — o tick roda pra todas as corretoras) e
      -- levanta AmbiguousParameter ANTES de olhar qualquer linha. Era o SEGUNDO
      -- bug segurando a mensagem planejada: o NameError (achado do Sol) estava
      -- no loop; este mata a consulta um passo antes. Achado 29/07.
      AND (CAST(:cid AS integer) IS NULL OR corretora_id = CAST(:cid AS integer))
      AND mensagem_planejada IS NOT NULL
      AND btrim(mensagem_planejada) <> ''
      AND msg_planejada_enviada_em IS NULL
      AND (
            (data_tarefa
             + COALESCE(hora_tarefa, TIME '{HORA_DEFAULT}'))
            AT TIME ZONE 'America/Sao_Paulo'
          ) <= now()
    ORDER BY data_tarefa ASC, hora_tarefa ASC NULLS FIRST, id ASC
"""

# Telefone do lead pelo lead_id (KOMMO) — fonte canônica leads_snapshot, com
# `leads` como rede. O espelho do Kommo NÃO tem todo mundo: lead que entrou pelo
# carrossel/remarketing vive só em `leads`. Sem a rede, a mensagem planejada
# desses leads era pulada calada (contava em `pulados_sem_telefone` e ninguém
# olhava). Achado 13/09/2026 com a Eloiza, que respondeu por áudio e ficaria de
# fora do envio de segunda.
_SQL_TELEFONE = """
    SELECT telefone FROM (
        SELECT 1 AS ordem, telefone FROM leads_snapshot
         WHERE lead_id = :lid AND telefone IS NOT NULL
        UNION ALL
        SELECT 2 AS ordem, telefone FROM leads
         WHERE external_ref = :lid AND telefone IS NOT NULL
    ) t ORDER BY ordem LIMIT 1
"""

# Dono do LEAD — fallback quando a tarefa veio sem corretora_id. Snapshot
# primeiro (é o que o painel mostra), espinha depois.
_SQL_DONO_DO_LEAD = """
    SELECT COALESCE(
        (SELECT corretora_id FROM leads_snapshot WHERE lead_id = :lid LIMIT 1),
        (SELECT owner_id FROM leads WHERE external_ref = :lid LIMIT 1)
    )
"""

# Opt-out compartilhado com o disparo de reengajamento: se o telefone (sufixo-8)
# pediu pra parar, NÃO manda. Mesma tabela/critério do fluxo da Pessoa12.
_SQL_OPT_OUT = r"""
    SELECT 1 FROM disparos_reengajamento
    WHERE right(regexp_replace(telefone, '\D', '', 'g'), 8) = :suf
      AND opt_out IS TRUE
    LIMIT 1
"""

# Cap diário COMPARTILHADO com a cadência na MESMA linha: soma as mensagens da
# cadência de hoje (cadencia_eventos tipo='mensagem') + as mensagens planejadas
# já disparadas hoje (msg_planejada_enviada_em::date). Ambas no fuso SP.
_SQL_CAP_HOJE = """
    SELECT
      (SELECT count(*) FROM cadencia_eventos
        WHERE tipo = 'mensagem'
          AND (at AT TIME ZONE 'America/Sao_Paulo')::date
              = (now() AT TIME ZONE 'America/Sao_Paulo')::date)
    + (SELECT count(*) FROM tarefas_corretora
        WHERE msg_planejada_enviada_em IS NOT NULL
          AND (msg_planejada_enviada_em AT TIME ZONE 'America/Sao_Paulo')::date
              = (now() AT TIME ZONE 'America/Sao_Paulo')::date)
    AS total
"""


def _so_digitos(tel: str | None) -> str:
    return "".join(c for c in (tel or "") if c.isdigit())


def _cap_atingido(session: Session) -> bool:
    total = session.execute(sql_text(_SQL_CAP_HOJE)).scalar() or 0
    return int(total) >= config_operacional.cap_diario()


def tick(
    agora: datetime | None = None,
    *,
    session: Session | None = None,
    session_factory: Callable[[], Session] | None = None,
    sender: Callable[..., dict[str, Any]] | None = None,
) -> MsgPlanejadaResult:
    """Roda 1 varredura de mensagens planejadas devidas. Idempotente
    (msg_planejada_enviada_em). Best-effort por tarefa.

    Args:
        sender: injeção do envio (default = enviar_template_corretor do cloud_corretora_a,
            atrás da trava DRY-RUN). Assinatura: (telefone, first_name, hook=...).
    """
    agora = agora or _now()
    # Dia útil (02/10/2026): mensagem planejada pra sábado/domingo sai no
    # primeiro tick de segunda (a tarefa continua devida). Ver dia_util.py.
    from app.services import dia_util

    if not dia_util.pode_enviar(agora, motor="msg_planejada"):
        return MsgPlanejadaResult()

    sender_injetado = sender
    if sender is None:
        from app.api.cloud_corretora_a import enviar_template_corretor as sender  # noqa: N806

    # dedupe leve da saudação (rede de segurança — o UI já trava o greeting).
    from app.api.cloud_corretora_a import _limpar_saudacao

    owns_session = session is None
    if owns_session:
        if session_factory is None:
            from app.db.session import SessionLocal

            session_factory = SessionLocal
        session = session_factory()

    res = MsgPlanejadaResult()

    # ── trava contra tick sobreposto (auditoria Grok+Sol, 29/07) ──
    # Mesmo padrão perigoso da cadência: busca `msg_planejada_enviada_em IS
    # NULL` → ENVIA na Meta → só então faz o UPDATE. Timer de 5 min; dois ticks
    # sobrepostos (execução anterior lenta, chamada manual) leem a MESMA tarefa
    # e o lead recebe 2×.
    trava = travar(NS_MSG_PLANEJADA, 1)
    if trava is OCUPADO:
        log.info("msg_planejada_tick_concorrente_pulou")
        if owns_session:
            session.close()
        return res

    try:
        devidas = session.execute(
            sql_text(_SQL_DEVIDAS), {"cid": None}
        ).mappings().all()
        res.considerados = len(devidas)

        for t in devidas:
            tarefa_id = t["id"]
            lead_id = t["lead_id"]
            corpo_raw = t["mensagem_planejada"] or ""
            try:
                tel_row = session.execute(
                    sql_text(_SQL_TELEFONE), {"lid": str(lead_id)}
                ).first()
                telefone = _so_digitos(tel_row[0]) if tel_row else ""
                if not telefone:
                    res.pulados_sem_telefone += 1
                    log.warning(
                        "msg_planejada_sem_telefone",
                        tarefa_id=tarefa_id, lead_id=lead_id,
                    )
                    continue

                suf = telefone[-8:]
                if session.execute(sql_text(_SQL_OPT_OUT), {"suf": suf}).first():
                    res.pulados_opt_out += 1
                    log.info(
                        "msg_planejada_opt_out",
                        tarefa_id=tarefa_id, telefone_suf=suf,
                    )
                    continue

                # cap compartilhado com a cadência: não estoura a linha junto.
                if _cap_atingido(session):
                    res.bloqueados_cap += 1
                    log.info("msg_planejada_cap_diario", tarefa_id=tarefa_id)
                    continue  # próximas tarefas do tick também estourariam — mas
                    # deixamos idempotência re-avaliar no próximo tick.

                corpo = _limpar_saudacao(corpo_raw)
                # UTILITY sempre (enviar_template_corretor só usa pool UTILITY). Greeting
                # PINADO: first_name=None → resolve nome pelo telefone e escolhe o
                # template determinístico (com nome=acme_ola / sem nome=acme_oi).
                # Linha da DONA da tarefa (Fase 2). Sem linha Cloud a tarefa é
                # PULADA — melhor não enviar do que enviar pelo número errado.
                #
                # Tarefa sem dono cai pro dono do LEAD (mesma fonte que o painel
                # usa pra mostrar o card). Não é chute: é a identidade canônica
                # do lead, e sem isso a tarefa fica presa pra sempre — a 100
                # (Eduarda/18-08) estava assim, com o lead da Pessoa12 bem definido.
                # Se nem o lead tem dono, aí sim pula.
                cid_tarefa = t["corretora_id"]
                if cid_tarefa is None:
                    cid_tarefa = session.execute(
                        sql_text(_SQL_DONO_DO_LEAD), {"lid": str(lead_id)}
                    ).scalar()
                    if cid_tarefa is not None:
                        log.info("msgplanejada_dono_herdado_do_lead",
                                 tarefa=tarefa_id, corretora_id=cid_tarefa)
                from app.services import cloud_lines as _cl

                linha = _cl.linha_da_corretora(cid_tarefa)
                if linha is _cl.INDETERMINADO:
                    linha = None
                if linha is None:
                    log.info("msgplanejada_sem_linha_cloud", tarefa=tarefa_id,
                             corretora_id=cid_tarefa)
                    continue
                if sender_injetado is not None:
                    resultado = sender(telefone, None, hook=corpo)
                else:
                    from app.api.cloud_corretora_a import enviar_template_linha

                    resultado = enviar_template_linha(
                        linha, telefone, None, hook=corpo,
                        instance=_cl.instance_da_linha(linha) if hasattr(_cl, "instance_da_linha") else None,
                        corretora_id=cid_tarefa,
                    )
                dry = bool(resultado.get("dry_run"))

                # marca enviada_em (idempotência) — vale inclusive em dry-run,
                # pra 2º tick não reenviar e pro smoke observar a marca.
                session.execute(
                    sql_text(
                        "UPDATE tarefas_corretora "
                        "SET msg_planejada_enviada_em = now() "
                        "WHERE id = :tid AND msg_planejada_enviada_em IS NULL"
                    ),
                    {"tid": tarefa_id},
                )
                session.commit()

                res.enviadas += 1
                if dry:
                    res.dry_run += 1
                res.detalhes.append({
                    "tarefa_id": int(tarefa_id),
                    "telefone_suf": suf,
                    "dry_run": dry,
                    "preview": corpo[:80],
                })
                log.info(
                    "msg_planejada_enviada",
                    tarefa_id=tarefa_id, lead_id=lead_id,
                    telefone_suf=suf, dry_run=dry,
                )
            except Exception as e:  # noqa: BLE001 — 1 falha não derruba o tick
                session.rollback()
                res.falhas += 1
                log.warning(
                    "msg_planejada_falha",
                    tarefa_id=tarefa_id, erro=str(e)[:160],
                )
    finally:
        # a trava sai POR ÚLTIMO: só depois do commit final outro tick entra.
        if owns_session:
            session.close()
        soltar(trava, NS_MSG_PLANEJADA, 1)

    log.info(
        "msg_planejada_tick_done",
        considerados=res.considerados,
        enviadas=res.enviadas,
        dry_run=res.dry_run,
        pulados_opt_out=res.pulados_opt_out,
        pulados_sem_telefone=res.pulados_sem_telefone,
        bloqueados_cap=res.bloqueados_cap,
        falhas=res.falhas,
    )
    return res
