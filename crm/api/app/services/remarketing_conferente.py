"""Conferente do remarketing — fecha a conta de cada dia. Código, sem IA.

## Por que existe

O disparo dizia "enviei" quando a Meta aceitava. Aceito não é entregue, e
ninguém fechava a conta no dia seguinte: os 519 envios de 15 a 19/09/2026
ficaram sem um único status de entrega gravado e isso só apareceu em 05/10,
num levantamento feito à mão.

O conferente roda sozinho (timer systemd, `scripts/remarketing_conferente.py`)
e, pra cada dia de São Paulo:

    previstos   o teto que valia naquele dia (o maior `cap_do_dia` das voltas ligadas)
    aceitos     disparos registrados (`carrossel_disparo`) — a Meta ACEITOU
    entregues   chegou no aparelho (`delivered` + `read`)
    lidos       `read`
    falhas      `failed`, com código e de quem é a culpa
    em_voo      aceito e ainda sem confirmação
    sem_rastro  aceito sem id da Meta — não dá pra saber
    respostas   por tipo: interesse, botão, recusa, parar, indefinida, automática

Grava em `remarketing_conferencia` (uma linha por dia; reconferir atualiza) e
levanta ALARMES — a lista está em `ALARMES`.

## De onde vem o status "da Meta"

A Cloud API **não tem** consulta de status por id de mensagem: o status só
chega por webhook. O que o conferente lê é o que o webhook gravou em
`mensagens_whatsapp`, cruzado pelo `wamid` do disparo. Por isso "nenhum status
chegou" é alarme próprio (`sem_status_da_meta`): pode ser entrega ruim, pode
ser o webhook da linha desligado — e nos dois casos alguém precisa olhar.

## O que mais ele faz

- **Reaplica status órfão** (`status_entrega.reaplicar_orfaos`): status que
  chegou antes de a mensagem estar gravada.
- **Concilia reserva órfã** (`conciliar_reservas`): a reserva é o "pode ter
  saído". Havendo PROVA de que saiu — o wamid guardado na própria reserva, ou
  a mensagem gravada no thread daquele telefone naqueles minutos —, a reserva
  vira disparo. Sem prova ela fica, marcada, e aparece na tela. Nunca é
  devolvida pra fila: sem prova de que NÃO saiu, reenviar é arriscar a segunda
  mensagem.

## Aviso no Telegram

`montar_aviso` + `avisar` estão prontos e **desligados**
(`remarketing_conferente_avisa_telegram`, padrão `false`). Envio externo é
decisão da Dona; quem liga é ela, pela configuração.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text as sql_text

from app.services import remarketing_placar as rp

log = structlog.get_logger()

TZ_SP = ZoneInfo("America/Sao_Paulo")
_FUSO = "America/Sao_Paulo"

# Reserva mais nova que isto ainda pode ser um envio em andamento.
IDADE_MINIMA_RESERVA_MIN = 10
# Janela em que a mensagem gravada no thread conta como "a desta reserva".
JANELA_ANTES_S = 60
JANELA_DEPOIS_MIN = 15

# Um dia útil do robô tem ~200 voltas (09:00–19:00, de 3 em 3 minutos).
VOLTAS_PRA_DIA_INTEIRO = 100
FRACAO_LIGADO_PRA_DIA_INTEIRO = 0.75
FALHA_ALTA_PCT = 20.0
FALHA_ALTA_MINIMO = 10
SEM_STATUS_MINIMO = 5

ALARMES: dict[str, str] = {
    "robo_nao_rodou":
        "o robô do disparo não rodou nenhuma vez neste dia",
    "ligado_sem_envio":
        "o disparo ficou ligado o dia inteiro e não mandou nenhuma mensagem",
    "sem_status_da_meta":
        "a Meta aceitou os envios e nenhum status de entrega chegou",
    "entrega_ruim":
        "muitas mensagens não chegaram",
    "envio_incerto":
        "há envio sem confirmação (não sei se saiu)",
    "reserva_sem_prova":
        "há lead marcado como disparado sem prova de que a mensagem saiu",
    "sem_rastro":
        "há envio aceito sem o identificador da Meta — não dá pra conferir a entrega",
}


def _hoje_sp(agora: datetime | None = None) -> date:
    return (agora or datetime.now(timezone.utc)).astimezone(TZ_SP).date()


# ── Reservas órfãs ───────────────────────────────────────────────────────────

_SQL_RESERVAS = """
    SELECT e.id, e.lead_id, e.at, e.payload, l.telefone, l.nome
      FROM lead_events e
      JOIN leads l ON l.id = e.lead_id
     WHERE e.tenant_id = :t AND e.tipo = :ev
       AND e.at < now() - make_interval(mins => :idade)
     ORDER BY e.at
"""

_SQL_MENSAGEM_DA_RESERVA = """
    SELECT m.evolution_message_id, m.conteudo_metadata
      FROM mensagens_whatsapp m
     WHERE m.instance = :inst
       AND m.from_corretora
       AND m.tipo_mensagem = 'template'
       AND m.fone8 = :suf
       AND m.evolution_message_id NOT LIKE 'dry-%'
       AND m.timestamp_evolution BETWEEN :de AND :ate
     ORDER BY m.timestamp_evolution
     LIMIT 1
"""


def _payload(bruto: Any) -> dict[str, Any]:
    if isinstance(bruto, str):
        try:
            bruto = json.loads(bruto)
        except ValueError:
            return {}
    return dict(bruto or {})


def _prova_de_envio(session: Any, payload: dict[str, Any], at: datetime,
                    telefone: str | None) -> dict[str, Any] | None:
    """O que prova que a mensagem desta reserva saiu. None = sem prova."""
    from app.services import cloud_lines

    if payload.get("wamid"):
        return {"como": "wamid_na_reserva", "wamid": payload["wamid"],
                "template": payload.get("template"),
                "categoria": payload.get("categoria")}
    suf = "".join(c for c in (telefone or "") if c.isdigit())[-8:]
    inst = cloud_lines.instance_do_slug(payload.get("linha") or "corretor")
    if len(suf) < 8 or not inst:
        return None
    linha = session.execute(
        sql_text(_SQL_MENSAGEM_DA_RESERVA),
        {"inst": inst, "suf": suf,
         "de": at - timedelta(seconds=JANELA_ANTES_S),
         "ate": at + timedelta(minutes=JANELA_DEPOIS_MIN)},
    ).first()
    if linha is None:
        return None
    meta = _payload(linha[1])
    wamid = str(linha[0] or "")
    return {"como": "mensagem_gravada",
            # Só id com cara de id da Meta serve pra conferir entrega; o resto
            # é chave fabricada pelo emissor pra gravar o thread.
            "wamid": wamid if wamid.startswith("wamid.") else None,
            "template": meta.get("template"), "categoria": meta.get("categoria")}


def conciliar_reservas(session: Any, tenant_id: int = 1) -> dict[str, Any]:
    """Reserva com prova de envio vira disparo; sem prova fica, marcada. Commita."""
    from app.services import carrossel_corretor as cb
    from app.services import carrossel_copies as cc

    linhas = session.execute(
        sql_text(_SQL_RESERVAS),
        {"t": tenant_id, "ev": cb.EVENT_RESERVA, "idade": IDADE_MINIMA_RESERVA_MIN},
    ).all()
    conciliadas = sem_prova = 0
    for eid, lead_id, at, bruto, telefone, _nome in linhas:
        payload = _payload(bruto)
        prova = _prova_de_envio(session, payload, at, telefone)
        if prova is None:
            if not payload.get("sem_prova_de_envio"):
                session.execute(
                    sql_text("UPDATE lead_events SET payload = payload || CAST(:x AS jsonb) "
                             " WHERE id = :i"),
                    {"i": eid, "x": json.dumps({
                        "sem_prova_de_envio": True,
                        "estado": payload.get("estado") or "sem_prova",
                        "conferido_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    })},
                )
            sem_prova += 1
            continue
        variante = payload.get("variante")
        extra: dict[str, Any] = {
            "wamid": prova["wamid"],
            "template": prova.get("template") or payload.get("template"),
            "categoria": prova.get("categoria") or payload.get("categoria") or "UTILITY",
            "conciliado": prova["como"],
            "conciliado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "estado_da_reserva": payload.get("estado"),
            "sem_prova_de_envio": False,
        }
        if not variante:
            # O balde do lead é determinístico (hash do id). Recalculado, e
            # marcado como tal: os pesos podem ter mudado desde o envio.
            try:
                variante = cc.escolher(lead_id).id
                extra["variante_recalculada"] = True
            except Exception:  # noqa: BLE001
                variante = None
        extra["variante"] = variante
        if prova["wamid"] is None:
            extra["sem_wamid"] = True
        session.execute(
            sql_text("UPDATE lead_events SET tipo = :ev, payload = payload || CAST(:x AS jsonb) "
                     " WHERE id = :i AND tipo = :reserva"),
            {"i": eid, "ev": cb.EVENT_DISPARO, "reserva": cb.EVENT_RESERVA,
             "x": json.dumps(extra, ensure_ascii=False)},
        )
        conciliadas += 1
        log.info("conferente_reserva_virou_disparo", lead_id=lead_id, como=prova["como"])
    session.commit()
    if sem_prova:
        log.warning("conferente_reservas_sem_prova", n=sem_prova)
    return {"conciliadas": conciliadas, "sem_prova": sem_prova}


def reservas_orfas(session: Any, tenant_id: int = 1, limite: int = 20) -> dict[str, Any]:
    """O que a tela mostra: leads em "Disparado" sem disparo registrado."""
    from app.services import carrossel_corretor as cb

    linhas = session.execute(
        sql_text(_SQL_RESERVAS),
        {"t": tenant_id, "ev": cb.EVENT_RESERVA, "idade": IDADE_MINIMA_RESERVA_MIN},
    ).all()
    por_estado: dict[str, int] = {}
    itens = []
    for _eid, lead_id, at, bruto, _tel, nome in linhas:
        p = _payload(bruto)
        estado = p.get("estado") or "aberta"
        por_estado[estado] = por_estado.get(estado, 0) + 1
        if len(itens) < limite:
            itens.append({"lead_id": int(lead_id), "nome": nome,
                          "desde": at.isoformat() if at else None,
                          "estado": estado, "motivo": p.get("motivo"),
                          "linha": p.get("linha")})
    return {
        "total": len(linhas),
        "por_estado": por_estado,
        "mais_antiga": linhas[0][2].isoformat() if linhas else None,
        "itens": itens,
        "o_que_e": (
            "lead que saiu da fila pra receber e não tem o disparo registrado: "
            "a mensagem pode ter saído ou não. Conta no teto do dia e descansa "
            "os 30 dias; não é reenviado sozinho."
        ),
    }


# ── A conta do dia ───────────────────────────────────────────────────────────

_SQL_DISPAROS_DO_DIA = """
    SELECT e.payload->>'wamid'             AS wamid,
           m.meta->>'delivery_status'      AS status,
           m.meta->>'delivery_error_code'  AS codigo,
           m.meta->>'delivery_error'       AS titulo
      FROM lead_events e
      LEFT JOIN LATERAL (
            SELECT w.conteudo_metadata AS meta
              FROM mensagens_whatsapp w
             WHERE w.evolution_message_id = e.payload->>'wamid'
             ORDER BY (w.conteudo_metadata ? 'delivery_status') DESC
             LIMIT 1
      ) m ON TRUE
     WHERE e.tenant_id = :t AND e.tipo = :ev
       AND (e.at AT TIME ZONE 'America/Sao_Paulo')::date = :dia
"""

_SQL_TICKS_DO_DIA = """
    SELECT count(*)                                              AS total,
           count(*) FILTER (WHERE ligado IS TRUE)                AS ligados,
           count(*) FILTER (WHERE ligado IS NULL)                AS sem_leitura,
           count(*) FILTER (WHERE enviados > 0)                  AS com_envio,
           coalesce(sum(enviados), 0)                            AS enviados,
           coalesce(sum(incertos), 0)                            AS incertos,
           coalesce(max(cap_do_dia) FILTER (WHERE ligado IS TRUE), 0) AS cap_max,
           min(em) AS primeiro, max(em) AS ultimo
      FROM remarketing_tick_log
     WHERE tenant_id = :t
       AND (em AT TIME ZONE 'America/Sao_Paulo')::date = :dia
"""

_SQL_MOTIVOS_DO_DIA = """
    SELECT coalesce(motivo_parada, 'enviou_ou_no_ritmo') AS motivo, count(*) AS n
      FROM remarketing_tick_log
     WHERE tenant_id = :t AND ligado IS TRUE
       AND (em AT TIME ZONE 'America/Sao_Paulo')::date = :dia
     GROUP BY 1 ORDER BY 2 DESC
"""

_SQL_RESPOSTAS_DO_DIA = """
    SELECT tipo, coalesce(payload->>'classe', 'sem_classe') AS classe, count(*) AS n
      FROM lead_events
     WHERE tenant_id = :t AND tipo IN (:ev_resp, :ev_auto)
       AND payload ? 'disparo_em'
       AND ((payload->>'disparo_em')::timestamptz AT TIME ZONE 'America/Sao_Paulo')::date = :dia
     GROUP BY 1, 2
"""

_SQL_RESERVAS_DO_DIA = """
    SELECT coalesce(payload->>'estado', 'aberta') AS estado, count(*) AS n
      FROM lead_events
     WHERE tenant_id = :t AND tipo = :ev
       AND (at AT TIME ZONE 'America/Sao_Paulo')::date = :dia
     GROUP BY 1
"""


def conta_do_dia(session: Any, dia: date, tenant_id: int = 1,
                 agora: datetime | None = None) -> dict[str, Any]:
    """Os números de um dia de São Paulo. Só leitura. Levanta se o banco
    falhar — zero por erro de leitura seria dizer "não falhou nada"."""
    from app.services import carrossel_corretor as cb

    hoje = _hoje_sp(agora)
    d: dict[str, Any] = {"dia": dia.isoformat(), "parcial": dia >= hoje}

    t = session.execute(sql_text(_SQL_TICKS_DO_DIA), {"t": tenant_id, "dia": dia}).mappings().one()
    d["voltas"] = {
        "total": int(t["total"]), "ligadas": int(t["ligados"]),
        "sem_leitura_do_painel": int(t["sem_leitura"]),
        "com_envio": int(t["com_envio"]),
        "primeira": t["primeiro"].isoformat() if t["primeiro"] else None,
        "ultima": t["ultimo"].isoformat() if t["ultimo"] else None,
        "motivos": {r[0]: int(r[1]) for r in session.execute(
            sql_text(_SQL_MOTIVOS_DO_DIA), {"t": tenant_id, "dia": dia}).all()},
    }
    d["previstos"] = int(t["cap_max"])

    baldes = {"entregues": 0, "em_voo": 0, "falhas": 0, "sem_rastro": 0}
    lidos = aceitos = 0
    motivos: dict[tuple[str | None, str | None], int] = {}
    for wamid, status, codigo, titulo in session.execute(
        sql_text(_SQL_DISPAROS_DO_DIA),
        {"t": tenant_id, "ev": cb.EVENT_DISPARO, "dia": dia},
    ).all():
        aceitos += 1
        balde = rp.balde_de(wamid, status)
        baldes[balde] += 1
        if (status or "").lower() == "read":
            lidos += 1
        if balde == "falhas":
            motivos[(codigo, titulo)] = motivos.get((codigo, titulo), 0) + 1
    rastreados = aceitos - baldes["sem_rastro"]
    d.update(aceitos=aceitos, lidos=lidos, **baldes)
    d["pct_entregue"] = round(100 * baldes["entregues"] / rastreados, 1) if rastreados else None
    d["pct_falha"] = round(100 * baldes["falhas"] / rastreados, 1) if rastreados else None
    d["motivos_de_falha"] = [
        {"codigo": c, "titulo": tt, "n": n, "culpa": rp.culpa_de(c, tt)}
        for (c, tt), n in sorted(motivos.items(), key=lambda kv: -kv[1])
    ]

    reservas = {r[0]: int(r[1]) for r in session.execute(
        sql_text(_SQL_RESERVAS_DO_DIA),
        {"t": tenant_id, "ev": cb.EVENT_RESERVA, "dia": dia}).all()}
    d["reservas_abertas"] = sum(reservas.values())
    d["reservas_por_estado"] = reservas
    d["incertos"] = reservas.get("incerto", 0)

    respostas = {c: 0 for c in ("interesse", "botao", "recusa", "parar", "indefinida",
                                "numero_errado", "sem_classe", "automatica")}
    for tipo, classe, n in session.execute(
        sql_text(_SQL_RESPOSTAS_DO_DIA),
        {"t": tenant_id, "dia": dia, "ev_resp": cb.EVENT_RESPOSTA,
         "ev_auto": cb.EVENT_AUTO_RESPOSTA},
    ).all():
        chave = "automatica" if tipo == cb.EVENT_AUTO_RESPOSTA else classe
        respostas[chave] = respostas.get(chave, 0) + int(n)
    d["respostas"] = respostas
    # O que conta como resposta na tela (interesse + botão); o resto é fora da taxa.
    d["respostas_que_contam"] = respostas["interesse"] + respostas["botao"]
    return d


def _log_de_voltas_ja_existia(session: Any, tenant_id: int, dia: date) -> bool:
    """Havia registro de volta ANTES deste dia? Sem isso, o dia em que a tabela
    nasceu (e os anteriores) pareceriam "o robô não rodou"."""
    return bool(session.execute(
        sql_text(
            "SELECT 1 FROM remarketing_tick_log WHERE tenant_id = :t "
            " AND (em AT TIME ZONE 'America/Sao_Paulo')::date < :dia LIMIT 1"
        ),
        {"t": tenant_id, "dia": dia},
    ).scalar())


def _dia_de_disparo(dia: date) -> bool:
    """O dia está na agenda gravada? Agenda ilegível conta como SIM: na dúvida
    o conferente cobra, não perdoa."""
    from app.services import remarketing_agenda as agenda

    try:
        dias = agenda.dias_semana()
    except Exception:  # noqa: BLE001
        return True
    return not dias or dia.weekday() in dias


def alarmes_do_dia(session: Any, d: dict[str, Any], dia: date,
                   tenant_id: int = 1) -> list[dict[str, Any]]:
    """Regras fixas em cima da conta do dia. Dia parcial (hoje) só levanta o
    que não depende de o dia ter acabado."""
    out: list[dict[str, Any]] = []

    def _add(codigo: str, detalhe: str) -> None:
        out.append({"codigo": codigo, "texto": ALARMES[codigo], "detalhe": detalhe})

    voltas = d["voltas"]
    fechado = not d["parcial"]
    if fechado and voltas["total"] == 0 and _log_de_voltas_ja_existia(session, tenant_id, dia):
        _add("robo_nao_rodou", "nenhuma volta registrada; conferir o timer "
                               "copiloto-cadencia-tick")
    if (fechado and d["aceitos"] == 0 and d["reservas_abertas"] == 0
            and voltas["total"] >= VOLTAS_PRA_DIA_INTEIRO
            and voltas["ligadas"] >= FRACAO_LIGADO_PRA_DIA_INTEIRO * voltas["total"]
            and _dia_de_disparo(dia)):
        principal = next(iter(voltas["motivos"]), "sem motivo registrado")
        _add("ligado_sem_envio",
             f"{voltas['ligadas']} voltas ligadas, nenhum envio; motivo mais "
             f"frequente: {principal}")
    if (fechado and d["aceitos"] - d["sem_rastro"] >= SEM_STATUS_MINIMO
            and d["entregues"] == 0 and d["falhas"] == 0):
        _add("sem_status_da_meta",
             f"{d['aceitos']} aceitos e nenhum status; conferir o webhook de "
             "status da linha")
    rastreados = d["aceitos"] - d["sem_rastro"]
    if (rastreados >= FALHA_ALTA_MINIMO and d["pct_falha"] is not None
            and d["pct_falha"] >= FALHA_ALTA_PCT):
        culpa = d["motivos_de_falha"][0]["culpa"] if d["motivos_de_falha"] else "desconhecida"
        _add("entrega_ruim", f"{d['falhas']} de {rastreados} falharam "
                             f"({d['pct_falha']}%); culpa mais comum: {culpa}")
    if d["incertos"]:
        _add("envio_incerto", f"{d['incertos']} envio(s) sem confirmação")
    sem_prova = d["reservas_abertas"] - d["incertos"]
    if fechado and sem_prova > 0:
        _add("reserva_sem_prova", f"{sem_prova} lead(s) em Disparado sem "
                                  "disparo registrado")
    if d["sem_rastro"]:
        _add("sem_rastro", f"{d['sem_rastro']} envio(s) sem id da Meta")
    return out


def conferir_dia(session: Any, dia: date, tenant_id: int = 1,
                 agora: datetime | None = None) -> dict[str, Any]:
    """Calcula e GRAVA a conferência do dia (uma linha por dia). Commita."""
    d = conta_do_dia(session, dia, tenant_id, agora)
    alarmes = alarmes_do_dia(session, d, dia, tenant_id)
    session.execute(
        sql_text(
            """INSERT INTO remarketing_conferencia (tenant_id, dia, dados, alarmes, conferido_em)
               VALUES (:t, :dia, CAST(:d AS jsonb), CAST(:a AS jsonb), now())
               ON CONFLICT (tenant_id, dia) DO UPDATE
                  SET dados = EXCLUDED.dados, alarmes = EXCLUDED.alarmes,
                      conferido_em = now()"""
        ),
        {"t": tenant_id, "dia": dia, "d": json.dumps(d, ensure_ascii=False),
         "a": json.dumps(alarmes, ensure_ascii=False)},
    )
    session.commit()
    if alarmes:
        log.warning("conferente_alarmes", dia=d["dia"],
                    alarmes=[a["codigo"] for a in alarmes])
    return {**d, "alarmes": alarmes}


def ultimas(session: Any, tenant_id: int = 1, dias: int = 7) -> list[dict[str, Any]]:
    """As conferências gravadas, da mais nova pra mais velha — o que a tela lê."""
    linhas = session.execute(
        sql_text(
            """SELECT dia, dados, alarmes, conferido_em, aviso_enviado_em
                 FROM remarketing_conferencia
                WHERE tenant_id = :t
                ORDER BY dia DESC LIMIT :n"""
        ),
        {"t": tenant_id, "n": int(dias)},
    ).all()
    return [
        {**_payload(r[1]),
         "alarmes": r[2] if isinstance(r[2], list) else json.loads(r[2] or "[]"),
         "conferido_em": r[3].isoformat() if r[3] else None,
         "aviso_enviado_em": r[4].isoformat() if r[4] else None}
        for r in linhas
    ]


# ── Aviso no Telegram: pronto, desligado ─────────────────────────────────────


def montar_aviso(c: dict[str, Any]) -> str:
    """O texto do aviso diário. Função pura."""
    dia = date.fromisoformat(c["dia"]).strftime("%d/%m")
    r = c["respostas"]
    linhas = [
        f"Remarketing, fechamento de {dia}",
        f"Previstos {c['previstos']} · aceitos {c['aceitos']} · "
        f"entregues {c['entregues']} · lidos {c['lidos']} · falhas {c['falhas']}",
    ]
    if c["em_voo"] or c["sem_rastro"]:
        linhas.append(f"Sem confirmação ainda: {c['em_voo']} · sem rastro: {c['sem_rastro']}")
    linhas.append(
        f"Respostas: {r['interesse']} com interesse · {r['botao']} no botão · "
        f"{r['recusa']} disseram não · {r['parar']} pediram pra parar · "
        f"{r.get('numero_errado', 0)} número errado · "
        f"{r['automatica']} automáticas · {r['indefinida'] + r['sem_classe']} sem sinal"
    )
    for a in c.get("alarmes") or []:
        linhas.append(f"ATENÇÃO: {a['texto']} ({a['detalhe']})")
    if not c.get("alarmes"):
        linhas.append("Nenhum alarme.")
    return "\n".join(linhas)


def avisar(session: Any, c: dict[str, Any], tenant_id: int = 1,
           enviar: Callable[[str], Any] | None = None) -> str:
    """Manda o fechamento do dia pra Dona — SE a chave estiver ligada.

    Devolve o que aconteceu: `desligado`, `dia_parcial`, `ja_avisado`,
    `sem_o_que_dizer`, `enviado`, `simulado` ou `falhou`. Um aviso por dia: o
    envio grava `aviso_enviado_em` e não repete.

    Só `enviado` grava a marca. O `TelegramTool` NÃO levanta quando o Telegram
    recusa: devolve o texto `error: …` (e `ok (dry_run)` em simulação). Tratar
    só exceção fazia a falha virar "aviso enviado", com a marca gravada e sem
    nova tentativa: o dia ficava sem aviso e o registro dizia o contrário.
    """
    from app.services import config_operacional

    try:
        ligado = config_operacional.get("remarketing_conferente_avisa_telegram") is True
    except config_operacional.ConfigIndisponivel:
        return "desligado"
    if not ligado:
        return "desligado"
    if c.get("parcial"):
        return "dia_parcial"
    if not c["aceitos"] and not c.get("alarmes"):
        # Dia sem disparo e sem alarme: silêncio. Aviso todo dia dizendo "nada"
        # ensina a ignorar o aviso.
        return "sem_o_que_dizer"
    dia = date.fromisoformat(c["dia"])
    ja = session.execute(
        sql_text("SELECT aviso_enviado_em FROM remarketing_conferencia "
                 " WHERE tenant_id = :t AND dia = :d"),
        {"t": tenant_id, "d": dia},
    ).scalar()
    if ja is not None:
        return "ja_avisado"
    if enviar is None:
        from app.tools.telegram import TelegramTool

        enviar = TelegramTool().send_to_dona
    try:
        retorno = enviar(montar_aviso(c))
    except Exception as e:  # noqa: BLE001
        log.error("conferente_aviso_nao_saiu", dia=c["dia"], erro=str(e)[:160])
        return "falhou"
    if isinstance(retorno, str):
        dito = retorno.strip().lower()
        if "dry_run" in dito:
            # Simulação: nada saiu. Não marca, pra o aviso de verdade sair
            # quando a simulação for desligada.
            log.info("conferente_aviso_simulado", dia=c["dia"])
            return "simulado"
        if not dito.startswith("ok"):
            log.error("conferente_aviso_nao_saiu", dia=c["dia"], erro=retorno[:160])
            return "falhou"
    session.execute(
        sql_text("UPDATE remarketing_conferencia SET aviso_enviado_em = now() "
                 " WHERE tenant_id = :t AND dia = :d"),
        {"t": tenant_id, "d": dia},
    )
    session.commit()
    log.info("conferente_aviso_enviado", dia=c["dia"])
    return "enviado"


# ── A rodada (o que o timer chama) ───────────────────────────────────────────


def rodar(tenant_id: int = 1, agora: datetime | None = None,
          dias_atras: int = 2, enviar: Callable[[str], Any] | None = None) -> dict[str, Any]:
    """Uma rodada: status órfão, reservas, e a conferência de hoje (parcial),
    ontem e anteontem — o status de entrega chega com atraso, então os dias
    fechados são reconferidos. Nunca levanta: devolve `erro`."""
    from app.db.session import SessionLocal
    from app.services import status_entrega

    hoje = _hoje_sp(agora)
    out: dict[str, Any] = {"hoje": hoje.isoformat(), "dias": [], "erro": None}
    try:
        with SessionLocal() as s:
            out["status_orfaos"] = status_entrega.reaplicar_orfaos(s)
            out["reservas"] = conciliar_reservas(s, tenant_id)
            for n in range(dias_atras, -1, -1):
                dia = hoje - timedelta(days=n)
                c = conferir_dia(s, dia, tenant_id, agora)
                aviso = avisar(s, c, tenant_id, enviar)
                out["dias"].append({"dia": c["dia"], "aceitos": c["aceitos"],
                                    "entregues": c["entregues"],
                                    "alarmes": [a["codigo"] for a in c["alarmes"]],
                                    "aviso": aviso})
    except Exception as e:  # noqa: BLE001
        log.exception("conferente_rodada_falhou")
        out["erro"] = f"{type(e).__name__}: {str(e)[:160]}"
    return out
