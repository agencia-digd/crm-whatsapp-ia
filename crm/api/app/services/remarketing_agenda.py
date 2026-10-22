"""Agenda do remarketing — QUANDO e QUANTO o carrossel da Corretor pode disparar.

## Por que existe

Até 04/10/2026 o carrossel tinha um interruptor (`carrossel_corretor_ativo`) e mais
nada que a Dona pudesse mexer: a hora vinha do timer do systemd, o dia era
"todo dia" e o volume era o que a rampa dissesse. Pedido dela: *"colocar que
horas que a gente quer que faça o disparo, que dia da semana, quantos disparos
por dia, se tem limite de disparo na semana"* — pela tela, e funcionando.

São quatro chaves em `config_operacional`, todas opcionais:

```
remarketing_janela_horario   "09:00-18:00"   None  = sem janela própria
remarketing_dias_semana      [0,1,2,3,4]     []    = todo dia (como era)
remarketing_cap_diario       40              None  = quem manda é a rampa (80)
remarketing_cap_semanal      200             None  = sem limite na semana
```

**Sem linha no banco, o comportamento é byte a byte o de antes.** É a regra da
casa (`config_operacional`): publicar isto não muda um disparo sequer até
alguém gravar um campo.

## O que cada trava protege

- **Janela e dias só SEGURAM.** Fora deles o tick sai sem enviar e loga o
  motivo. Nada se perde: o lead continua na Fila e o próximo tick dentro da
  janela pega.
- **O limite por dia é um PEDIDO; quem decide é a rampa.** Vale sempre o
  menor entre o número da tela e o teto da rampa (`app/services/rampa.py`).
  Até 80 o campo só baixa. Acima de 80 (até 300) a rampa só acompanha com a
  linha **GREEN** na Meta — com qualquer outra leitura o teto segue 80 e o
  `motivo` da rampa diz por quê. Um número digitado nunca fura o freio de
  qualidade: YELLOW continua 50, RED continua zero.
- **Número pedido ≠ número que sai.** `teto_efetivo()` devolve os dois e a
  explicação; é o que a tela mostra. Até 05/10 dava pra gravar 200 e o motor
  mandava 80 sem avisar ninguém.
- **O cap semanal soma com o diário** — o que estourar primeiro trava.
- **Erro nunca libera volume.** Valor ilegível no banco = segura. Contagem da
  semana que falha = assume cheio.

## O timer manda por fora

O tick do carrossel é chamado pelo `copiloto-cadencia-tick.timer`, que só roda
das 09:00 às 18:57 de São Paulo (`OnCalendar=*-*-* 12..21:00/3:00 UTC`). Uma
janela "20:00-22:00" seria aceita e nunca dispararia — por isso a validação
recusa janela que não caiba no horário do timer. Mudou o timer, muda
`TIMER_INICIO`/`TIMER_FIM` aqui.

## Lote manual

`scripts/carrossel_burst.py` chama o tick com `ignorar_ritmo=True` (e às vezes
`cap_override`). É lote autorizado pela Dona na hora, fora da rotina: janela e
dias são ritmo e saem com `ignorar_ritmo`; os caps do painel saem com
`cap_override`, igual o freio amarelo da rampa já saía.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text as sql_text

log = structlog.get_logger()

TZ_SP = ZoneInfo("America/Sao_Paulo")

# Horário em que o timer do systemd acorda o tick (minutos desde 00:00, SP).
TIMER_INICIO = 9 * 60
TIMER_FIM = 19 * 60

# 300/dia é a capacidade pedida pela Dona em 05/10/2026 (era 250, o tier
# inicial da Cloud API). É o MÁXIMO ACEITO no campo, não o que sai: quem
# decide o que sai é a rampa (`rampa.DEGRAU_MAXIMO`, que tem que acompanhar
# este número). A semana tem 7 dias disso, no máximo.
CAP_DIARIO_MAXIMO = 300
CAP_SEMANAL_MAXIMO = CAP_DIARIO_MAXIMO * 7

DIAS_ROTULO = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")

_RE_JANELA = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def _hhmm(minutos: int) -> str:
    return f"{minutos // 60:02d}:{minutos % 60:02d}"


# ── Validação (o que a tela grava passa por aqui) ────────────────────────────
# Levanta ValueError com mensagem em PT-BR; `api/automacoes._validar_config`
# transforma em 422. A validação mora no serviço e não no endpoint pra o tick
# poder usar os MESMOS parsers ao ler — um formato, um lugar.


def parse_janela(valor: Any) -> tuple[int, int]:
    """"09:00-18:00" → (540, 1080), em minutos do dia de São Paulo."""
    if not isinstance(valor, str):
        raise ValueError("a janela precisa ser um texto no formato 09:00-18:00")
    m = _RE_JANELA.match(valor)
    if not m:
        raise ValueError(
            f"horário inválido: {valor!r} — use o formato 09:00-18:00"
        )
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        raise ValueError(f"horário inválido: {valor!r} — hora vai de 00:00 a 23:59")
    ini, fim = h1 * 60 + m1, h2 * 60 + m2
    if ini >= fim:
        raise ValueError(
            "a janela precisa começar antes de terminar (ex.: 09:00-18:00)"
        )
    return ini, fim


def validar_janela(valor: Any) -> str | None:
    """Normaliza pra "HH:MM-HH:MM". None/vazio = sem janela própria."""
    if valor is None or (isinstance(valor, str) and not valor.strip()):
        return None
    ini, fim = parse_janela(valor)
    if ini < TIMER_INICIO or fim > TIMER_FIM:
        raise ValueError(
            f"a janela precisa caber entre {_hhmm(TIMER_INICIO)} e "
            f"{_hhmm(TIMER_FIM)} — fora disso o robô do disparo não roda e a "
            "mensagem nunca sairia"
        )
    return f"{_hhmm(ini)}-{_hhmm(fim)}"


def validar_dias(valor: Any) -> list[int]:
    """Lista de dias 0–6 (seg=0), sem repetição, ordenada. [] = todo dia."""
    if valor is None:
        return []
    if not isinstance(valor, list):
        raise ValueError("esperado uma lista de dias da semana (0=seg … 6=dom)")
    dias: list[int] = []
    for d in valor:
        if not isinstance(d, int) or isinstance(d, bool) or not (0 <= d <= 6):
            raise ValueError(
                f"dia da semana inválido: {d!r} — vai de 0 (segunda) a 6 (domingo)"
            )
        dias.append(d)
    if len(set(dias)) != len(dias):
        raise ValueError("dia da semana repetido")
    return sorted(dias)


def _validar_cap(valor: Any, *, nome: str, maximo: int) -> int | None:
    if valor is None:
        return None
    if not isinstance(valor, int) or isinstance(valor, bool):
        raise ValueError(f"{nome} precisa ser número inteiro")
    if valor < 0:
        raise ValueError(f"{nome} não pode ser negativo")
    if valor > maximo:
        raise ValueError(f"{nome} fora da faixa (0 a {maximo})")
    return valor


def validar_cap_diario(valor: Any) -> int | None:
    """None = quem manda é a rampa. Número = teto do dia (a rampa ainda vale)."""
    return _validar_cap(valor, nome="o limite por dia", maximo=CAP_DIARIO_MAXIMO)


def validar_cap_semanal(valor: Any) -> int | None:
    """None = sem limite na semana."""
    return _validar_cap(valor, nome="o limite por semana", maximo=CAP_SEMANAL_MAXIMO)


# ── Leitura (o que o tick e a tela usam) ─────────────────────────────────────


class ConfigIlegivel(ValueError):
    """Valor gravado no banco que não passa na validação (alguém escreveu por
    fora da tela). O tick trata como SEGURA: não dá pra saber o que a pessoa
    quis dizer, e adivinhar pra cima é como se perde um número."""


def _ler(chave: str, validar) -> Any:
    from app.services import config_operacional

    try:
        return validar(config_operacional.get(chave))
    except ValueError as e:
        raise ConfigIlegivel(f"{chave}: {e}") from e


def janela() -> tuple[int, int] | None:
    txt = _ler("remarketing_janela_horario", validar_janela)
    return parse_janela(txt) if txt else None


def dias_semana() -> list[int]:
    return _ler("remarketing_dias_semana", validar_dias)


def cap_diario() -> int | None:
    return _ler("remarketing_cap_diario", validar_cap_diario)


def cap_semanal() -> int | None:
    return _ler("remarketing_cap_semanal", validar_cap_semanal)


def _agora_sp(agora: datetime | None) -> datetime:
    agora = agora or datetime.now(timezone.utc)
    if agora.tzinfo is None:
        agora = agora.replace(tzinfo=timezone.utc)
    return agora.astimezone(TZ_SP)


def dentro_do_horario_do_robo(agora: datetime | None = None) -> bool:
    """Está dentro do horário em que o timer acorda o tick (09:00–19:00 SP)?

    O timer só limita quando a volta COMEÇA. Uma volta que começa 18:57 e
    demora (rotação de templates, linha lenta) mandaria a terceira mensagem
    depois das 19h. A autorização final de cada mensagem pergunta isto.
    """
    sp = _agora_sp(agora)
    return TIMER_INICIO <= sp.hour * 60 + sp.minute < TIMER_FIM


def bloqueio_de_agenda(agora: datetime | None = None) -> str | None:
    """Motivo pelo qual o tick NÃO pode enviar agora, ou None se pode.

    Só olha dia e hora — os caps dependem de contagem no banco e ficam no tick.
    Dia vem antes de hora: "hoje não é dia" é a resposta mais útil num sábado.
    """
    sp = _agora_sp(agora)
    try:
        dias = dias_semana()
        jan = janela()
    except ConfigIlegivel as e:
        log.error("remarketing_agenda_ilegivel_segurando", erro=str(e)[:160])
        return "agenda_ilegivel"
    if dias and sp.weekday() not in dias:
        return "fora_dos_dias"
    if jan is not None:
        minutos = sp.hour * 60 + sp.minute
        if not (jan[0] <= minutos < jan[1]):
            return "fora_da_janela"
    return None


def enviados_na_semana(session, tenant_id: int, evento: str | tuple[str, ...]) -> int:
    """Disparos do carrossel na semana corrente — segunda 00:00 a domingo, SP.

    `evento` aceita um tipo ou vários: o tick passa o disparo registrado E a
    reserva que sobrou (mensagem que pode ter saído sem o registro fechar).

    `date_trunc('week')` do Postgres é ISO: a semana começa na segunda. Trunca
    o relógio de São Paulo, não o UTC — no domingo à noite as duas semanas
    discordam e o saldo zeraria três horas antes da hora.
    """
    try:
        return int(session.execute(
            sql_text(
                """
                SELECT count(*) FROM lead_events
                 WHERE tenant_id = :t AND tipo = ANY(:evs)
                   AND (at AT TIME ZONE 'America/Sao_Paulo')
                       >= date_trunc('week', now() AT TIME ZONE 'America/Sao_Paulo')
                """
            ),
            {"t": tenant_id,
             "evs": [evento] if isinstance(evento, str) else list(evento)},
        ).scalar() or 0)
    except Exception as e:  # noqa: BLE001
        # Mesma regra de `carrossel_corretor._enviados_hoje`: não deu pra contar →
        # assume cheio. Erro nunca libera volume.
        log.warning("remarketing_contagem_semana_falhou_assumindo_cheio",
                    erro=str(e)[:120])
        return 10**6


def teto_efetivo(pedido: int | None, cap_rampa: int | None,
                 motivo_rampa: str | None,
                 cabe_no_horario: int | None = None) -> dict[str, Any]:
    """O número que VALE hoje, o que foi pedido, e por que os dois diferem.

    Mesma conta do tick (`carrossel_corretor.tick`): o menor entre o pedido e a
    rampa. `cap_rampa` precisa ter sido calculado COM o pedido
    (`rampa.cap_de_hoje(..., pedido=pedido)`) — senão a rampa responde 80 pra
    um pedido de 300 com a linha GREEN e a tela mentiria pro outro lado.

    `cabe_no_horario` é quanto dá pra enviar no horário configurado
    (`carrossel_corretor.maximo_que_cabe_no_dia`). Quando é menor que o teto, é ELE
    o número que vale — e a explicação diz que o freio é o horário.

    `efetivo: None` = não consegui ler a rampa (nunca um número inventado).
    """
    if cap_rampa is None:
        return {"pedido": pedido, "rampa": None, "efetivo": None, "diverge": False,
                "explicacao": "não consegui ler o teto da rampa agora"}
    efetivo = cap_rampa if pedido is None else min(cap_rampa, pedido)
    diverge = pedido is not None and efetivo < pedido
    if diverge:
        explicacao = (f"você pediu {pedido} por dia, mas hoje saem no máximo "
                      f"{efetivo}: {motivo_rampa or 'é o teto da rampa'}")
    elif pedido is None:
        explicacao = f"sem limite próprio — vale o teto da rampa ({cap_rampa})"
    else:
        explicacao = f"vale o que você pediu ({pedido})"
    if cabe_no_horario is not None and cabe_no_horario < efetivo:
        explicacao = (f"o teto de hoje é {efetivo}, mas no horário configurado "
                      f"cabem no máximo {cabe_no_horario} (saem 3 a cada 3 minutos) "
                      "— aumente o horário ou conte com esse número")
        efetivo, diverge = cabe_no_horario, True
    return {"pedido": pedido, "rampa": cap_rampa, "efetivo": efetivo,
            "diverge": diverge, "explicacao": explicacao,
            "cabe_no_horario": cabe_no_horario}


MOTIVO_EM_PORTUGUES: dict[str, str] = {
    "config_indisponivel": "não consegui ler a configuração no banco, segurando",
    "autorizacao_indisponivel": "não consegui conferir o painel antes do envio, segurando",
    "fora_do_horario_do_robo": "fora do horário em que o robô do disparo roda (09:00 às 19:00)",
    "desligado_no_meio_do_lote": "o disparo foi desligado no meio do lote",
    "alvo_mudou_no_meio_do_lote": "a etapa do disparo mudou no meio do lote",
    "linha_mudou_no_meio_do_lote": "o número disparador mudou no meio do lote",
    "envio_incerto": "um envio ficou sem confirmação, o lote parou",
    "pesos_invalidos": "os pesos do teste A/B gravados não fecham, segurando",
    "copy_invalida": "o texto de uma variante gravado não serve, segurando",
    "fila_vazia": "não há lead elegível na etapa escolhida",
    "fora_dos_dias": "hoje não é dia de disparo",
    "fora_da_janela": "fora do horário de disparo",
    "agenda_ilegivel": "a configuração de horário está ilegível — segurando",
    "cap_semanal_atingido": "o limite da semana já foi usado",
    "cap_do_dia_atingido": "o limite de hoje já foi usado",
}


def resumo(session, tenant_id: int, evento: str,
           agora: datetime | None = None) -> dict[str, Any]:
    """O que a tela mostra da agenda: o configurado, o gasto e o saldo.

    Teto sem saldo não ajuda ninguém a decidir — por isso `enviados_semana` e
    `saldo_semana` vão junto. `enviados_semana: None` = não consegui contar
    (nunca zero).
    """
    out: dict[str, Any] = {
        "timer": f"{_hhmm(TIMER_INICIO)}-{_hhmm(TIMER_FIM)}",
        "ilegivel": None,
    }
    try:
        jan = janela()
        out["janela_horario"] = f"{_hhmm(jan[0])}-{_hhmm(jan[1])}" if jan else None
        out["dias_semana"] = dias_semana()
        out["cap_diario"] = cap_diario()
        out["cap_semanal"] = cap_semanal()
    except ConfigIlegivel as e:
        out.update(janela_horario=None, dias_semana=[], cap_diario=None,
                   cap_semanal=None, ilegivel=str(e)[:160])

    n = enviados_na_semana(session, tenant_id, evento)
    contou = n < 10**6
    out["enviados_semana"] = n if contou else None
    out["enviados_semana_indisponivel"] = not contou
    teto = out["cap_semanal"]
    out["saldo_semana"] = (
        max(0, teto - n) if (teto is not None and contou) else None
    )

    bloq = "agenda_ilegivel" if out["ilegivel"] else bloqueio_de_agenda(agora)
    if bloq is None and teto is not None and (not contou or n >= teto):
        bloq = "cap_semanal_atingido"
    out["bloqueio_agora"] = bloq
    out["bloqueio_agora_texto"] = MOTIVO_EM_PORTUGUES.get(bloq) if bloq else None
    return out
