"""Vigia de automação dormindo.

Pra cada timer de envio do copiloto, confere quando foi o último tick. Se
passou de 2x o ritmo do timer, avisa a Dona (bridge Telegram da Assistente, o mesmo
canal dos outros avisos internos: `app.tools.notify.notify_dona`). NENHUM lead
recebe nada: é só aviso interno.

O que NÃO é "dormindo":
  - fora da janela de horário do timer (conta a partir da abertura da janela);
  - fim de semana, nos motores que não enviam sábado/domingo
    (só se o interruptor `envio_dias_uteis` estiver ligado);
  - interruptor do motor desligado de propósito (`config_operacional`);
  - timer desativado no systemd (`systemctl disable --now`, ex.: obra 4).

Não repete: o estado (arquivo JSON) guarda a hora do último aviso por timer;
só reavisa depois de `REAVISO_HORAS` (6 h). Timer que volta ao normal sai do
estado, então uma nova queda avisa de novo na hora.

A lógica é pura (`avaliar`, `rodar`): relógio, leitura do systemd, interruptores
e envio entram por parâmetro, pra testar sem systemd, sem banco e sem Telegram.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import structlog

log = structlog.get_logger()

SP_TZ = ZoneInfo("America/Sao_Paulo")
REAVISO_HORAS = 6
FATOR_TOLERANCIA = 2

OK = "ok"
DORMINDO = "dormindo"
FORA_DA_JANELA = "fora_da_janela"
FIM_DE_SEMANA = "fim_de_semana"
DESLIGADO = "desligado"
DESATIVADO = "desativado"


@dataclass(frozen=True)
class TimerVigiado:
    nome: str                         # unit sem ".timer"
    ritmo_min: int                    # intervalo normal entre ticks
    janela_utc: tuple[int, int] | None = None   # horas UTC (ini, fim) inclusivas; None = 24 h
    dias_uteis: bool = False          # motor não envia sábado/domingo
    interruptor: str | None = None    # chave de config_operacional (False = desligado de propósito)


# Ritmo e janela copiados do OnCalendar das units instaladas (21/10/2026).
TIMERS: tuple[TimerVigiado, ...] = (
    TimerVigiado("copiloto-cadencia-tick", 3, (12, 21), True, "cadencia_ativa"),
    TimerVigiado("copiloto-msgplanejada-tick", 5, (12, 21), True),
    TimerVigiado("copiloto-antighosting", 15, None, False, "anti_ghosting_ativo"),
    TimerVigiado("copiloto-vigia-silencio", 15, (11, 22)),
    TimerVigiado("copiloto-noshow", 60, None, False, "no_show_ativo"),
    TimerVigiado("copiloto-lembrete-visita-d1", 10, None, False, "lembrete_visita_d1_ativo"),
    TimerVigiado("copiloto-saida-remarketing", 60),
    TimerVigiado("acme-nutricao-watcher", 1, None, True, "nutricao_ativa"),
    TimerVigiado("acme-reeng-respostas", 5),
)


@dataclass(frozen=True)
class Leitura:
    ativo: bool
    ultimo_tick: datetime | None      # UTC, aware


@dataclass(frozen=True)
class Veredito:
    timer: str
    estado: str
    parado_min: int = 0               # minutos sem tick (só em DORMINDO)


def avaliar(t: TimerVigiado, leitura: Leitura, agora: datetime,
            interruptor: Callable[[str], bool], dias_uteis_ligado: bool) -> Veredito:
    agora = agora.astimezone(timezone.utc)
    if not leitura.ativo:
        return Veredito(t.nome, DESATIVADO)
    if t.interruptor and not interruptor(t.interruptor):
        return Veredito(t.nome, DESLIGADO)
    if t.dias_uteis and dias_uteis_ligado and agora.astimezone(SP_TZ).weekday() >= 5:
        return Veredito(t.nome, FIM_DE_SEMANA)

    referencia = leitura.ultimo_tick or datetime.fromtimestamp(0, timezone.utc)
    if t.janela_utc:
        ini, fim = t.janela_utc
        abre = agora.replace(hour=ini, minute=0, second=0, microsecond=0)
        fecha = agora.replace(hour=fim, minute=59, second=59, microsecond=0)
        if not (abre <= agora <= fecha):
            return Veredito(t.nome, FORA_DA_JANELA)
        referencia = max(referencia, abre)   # a conta começa quando a janela abre

    parado = agora - referencia
    if parado > timedelta(minutes=FATOR_TOLERANCIA * t.ritmo_min):
        return Veredito(t.nome, DORMINDO, int(parado.total_seconds() // 60))
    return Veredito(t.nome, OK)


def ler_systemd(nome: str) -> Leitura:
    """`systemctl show` do timer. Só leitura."""
    out = subprocess.run(
        ["systemctl", "show", f"{nome}.timer", "-p", "ActiveState", "-p", "LastTriggerUSec"],
        capture_output=True, text=True, timeout=10, check=True, env={"TZ": "UTC", "PATH": "/usr/bin:/bin"},
    ).stdout
    campos = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    bruto = campos.get("LastTriggerUSec", "").strip()
    tick = None
    if bruto:
        # "Wed 2026-10-21 21:57:00 UTC" (TZ forçado a UTC acima)
        tick = datetime.strptime(" ".join(bruto.split()[1:3]), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return Leitura(ativo=campos.get("ActiveState") == "active", ultimo_tick=tick)


def _carregar(estado: Path) -> dict[str, str]:
    try:
        return json.loads(estado.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _gravar(estado: Path, dados: dict[str, str]) -> None:
    estado.parent.mkdir(parents=True, exist_ok=True)
    tmp = estado.with_suffix(".tmp")
    tmp.write_text(json.dumps(dados, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(estado)


def montar_aviso(dormindo: list[Veredito]) -> str:
    linhas = [f"• `{v.timer}`: sem tick há {v.parado_min} min" for v in dormindo]
    return ("⚠️ *Automação dormindo* (aviso interno, nenhum lead recebeu nada)\n\n"
            + "\n".join(linhas)
            + "\n\nConferir: `systemctl status <timer>` e `journalctl -u <unit> -n 30`. "
              f"Reavisa só daqui a {REAVISO_HORAS} h se continuar parado.")


def rodar(agora: datetime, estado: Path, *,
          ler: Callable[[str], Leitura],
          interruptor: Callable[[str], bool],
          dias_uteis_ligado: bool,
          enviar: Callable[[str, str], bool],
          timers: tuple[TimerVigiado, ...] = TIMERS) -> list[Veredito]:
    """Uma volta do vigia. `enviar(task_id, texto)` devolve True se o aviso saiu."""
    agora = agora.astimezone(timezone.utc)
    avisados = _carregar(estado)
    veredictos: list[Veredito] = []
    a_avisar: list[Veredito] = []

    for t in timers:
        try:
            v = avaliar(t, ler(t.nome), agora, interruptor, dias_uteis_ligado)
        except Exception as exc:  # noqa: BLE001 — um timer ilegível não derruba os outros
            log.warning("vigia_leitura_falhou", timer=t.nome, erro=str(exc)[:120])
            continue
        veredictos.append(v)
        if v.estado == OK:
            avisados.pop(t.nome, None)          # voltou: próxima queda avisa na hora
        elif v.estado == DORMINDO:
            ultimo = avisados.get(t.nome)
            if not ultimo or agora - datetime.fromisoformat(ultimo) >= timedelta(hours=REAVISO_HORAS):
                a_avisar.append(v)

    if a_avisar:
        task_id = f"copiloto-vigia-automacao-{agora:%Y%m%d%H%M}"
        if enviar(task_id, montar_aviso(a_avisar)):
            for v in a_avisar:
                avisados[v.timer] = agora.isoformat()
            log.warning("vigia_aviso_enviado", timers=[v.timer for v in a_avisar])
        else:
            log.warning("vigia_aviso_nao_saiu", timers=[v.timer for v in a_avisar])
    _gravar(estado, avisados)
    return veredictos
