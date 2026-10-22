"""Vigia do SILÊNCIO — lead de anúncio que entrou e não recebeu NENHUMA mensagem.

Porte de `projetos/crm-dex/scripts/vigia-silencio.sh` (02/10/2026). Na DEX ele
nasceu de 16 leads que ficaram cinco semanas sem primeira mensagem sem gerar
erro, log nem alarme — quem descobriu foi o cliente, contando número por número
num print.

Na Acme o sintoma nunca apareceu até aqui (02/10: 383 de 383 leads
`meta_leadgen` dos últimos 60 dias receberam a primeira mensagem em menos de
30 min). Este vigia não é conserto de bug: é o alarme do SINTOMA, que é o mesmo
qualquer que seja a causa — intake desligado, linha sem template, roleta vazia,
Graph fora, boas-vindas que falhou. Vigia de sintoma pega causa que ainda não
existe.

Regra:
  - lead do tenant 1 com origem de anúncio (`meta_leadgen`; env
    `VIGIA_SILENCIO_ORIGENS` troca a lista), telefone válido;
  - criado há mais de `IDADE_MIN` (30 min) e há menos de `JANELA_H` (72 h —
    lead mais velho que isso já foi avisado muitas vezes, e a base antiga não
    pode virar ruído);
  - ZERO mensagem de saída (`mensagens_whatsapp.from_corretora`) pro mesmo
    telefone (sufixo-8, `fone8`) a partir da criação do lead.
  - Com dona ou SEM dona: lead sem dona é justamente o caso "roleta vazia".

Aviso: Telegram da Dona pelo mesmo canal dos alertas do copiloto
(`app.tools.telegram.TelegramTool.send_to_dona`, o de `alerta_entrega` e do
sinal de negócio). Não manda nada pra lead.

Cooldown: o mesmo conjunto de leads só é reavisado depois de `COOLDOWN_H` (2 h);
lead NOVO no silêncio avisa na hora. A marca de "já avisei" só é gravada depois
de o Telegram responder `ok` — gravar sem ter avisado cala o vigia pelo
cooldown inteiro (bug real do `acme-muda-watch`, lembrado no vigia da DEX).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text as sql_text

log = structlog.get_logger()

TZ_SP = ZoneInfo("America/Sao_Paulo")
IDADE_MIN = int(os.getenv("VIGIA_SILENCIO_IDADE_MIN", "30"))
JANELA_H = int(os.getenv("VIGIA_SILENCIO_JANELA_H", "72"))
COOLDOWN_H = float(os.getenv("VIGIA_SILENCIO_COOLDOWN_H", "2"))
ORIGENS = tuple(
    o.strip() for o in os.getenv("VIGIA_SILENCIO_ORIGENS", "meta_leadgen").split(",")
    if o.strip()
)
# Horário de vigia (SP). Fora dele o timer nem roda; o script confere de novo
# pra uma execução manual fora de hora não acordar a Dona.
HORA_INICIO, HORA_FIM = 8, 20
ESTADO = os.getenv("VIGIA_SILENCIO_ESTADO", "/var/lib/copiloto/vigia_silencio.json")

# Tolerância pra boas-vindas gravada segundos ANTES do `created_at` do lead (o
# envio e o INSERT do lead não são a mesma transação).
_TOLERANCIA = "5 minutes"

SQL_SILENCIO = f"""
WITH l AS (
  SELECT l.id, l.nome, l.owner_id, l.origem, l.created_at,
         -- mesma expressão da coluna gerada `fone8` (que o banco de teste não tem)
         right(regexp_replace(coalesce(l.telefone, ''), '\\D', '', 'g'), 8) AS f8
    FROM leads l
   WHERE l.tenant_id = 1
     AND l.origem = ANY(CAST(:origens AS text[]))
     AND coalesce(l.telefone_invalido, false) = false
     AND length(regexp_replace(coalesce(l.telefone, ''), '\\D', '', 'g')) >= 10
     AND l.created_at <  CAST(:agora AS timestamptz) - make_interval(mins => :idade_min)
     AND l.created_at >= CAST(:agora AS timestamptz) - make_interval(hours => :janela_h)
)
SELECT l.id, coalesce(l.nome, '(sem nome)') AS nome, l.owner_id,
       coalesce(c.nome, '(sem dona)') AS dona, l.origem, l.created_at
  FROM l
  LEFT JOIN corretoras c ON c.id = l.owner_id
 WHERE NOT EXISTS (
         SELECT 1 FROM mensagens_whatsapp m
          WHERE m.fone8 = l.f8
            AND m.from_corretora
            AND coalesce(m.timestamp_evolution, m.recebido_em)
                >= l.created_at - interval '{_TOLERANCIA}'
            -- `<= agora`: no replay do histórico, mensagem que saiu DEPOIS do
            -- instante simulado ainda não existia.
            AND coalesce(m.timestamp_evolution, m.recebido_em)
                <= CAST(:agora AS timestamptz))
 ORDER BY l.created_at
 LIMIT 30
"""


@dataclass
class LeadMudo:
    id: int
    nome: str
    dona: str
    origem: str
    created_at: datetime

    def minutos(self, agora: datetime) -> int:
        ca = self.created_at
        if ca.tzinfo is None:
            ca = ca.replace(tzinfo=timezone.utc)
        return int((agora - ca).total_seconds() // 60)


def procurar(session: Any, agora: datetime | None = None, *,
             origens: Iterable[str] = ORIGENS, idade_min: int = IDADE_MIN,
             janela_h: int = JANELA_H) -> list[LeadMudo]:
    """Leads em silêncio no instante `agora` (injetável: replay do histórico)."""
    agora = agora or datetime.now(timezone.utc)
    linhas = session.execute(
        sql_text(SQL_SILENCIO),
        {"origens": list(origens), "agora": agora, "idade_min": int(idade_min),
         "janela_h": int(janela_h)},
    ).mappings().all()
    return [LeadMudo(id=int(r["id"]), nome=r["nome"], dona=r["dona"],
                     origem=r["origem"], created_at=r["created_at"]) for r in linhas]


def no_horario(agora: datetime | None = None) -> bool:
    agora = agora or datetime.now(timezone.utc)
    h = agora.astimezone(TZ_SP).hour
    return HORA_INICIO <= h < HORA_FIM


def como_texto(leads: list[LeadMudo], agora: datetime) -> str:
    linhas = []
    for l in leads[:15]:
        m = l.minutos(agora)
        tempo = f"{m // 60}h{m % 60:02d}" if m >= 60 else f"{m} min"
        linhas.append(f"• {l.nome} (lead {l.id}) — {l.dona} · {tempo} sem mensagem")
    resto = f"\n… e mais {len(leads) - 15}" if len(leads) > 15 else ""
    return (
        f"🔇 Acme: {len(leads)} lead(s) de anúncio sem NENHUMA mensagem "
        f"enviada (mais de {IDADE_MIN} min).\n"
        + "\n".join(linhas) + resto
        + "\n\nÉ o sintoma, não a causa: conferir intake/boas-vindas ligado, "
        "roleta, linha da dona e template."
    )


# ── estado do cooldown ───────────────────────────────────────────────────────
def ler_estado(caminho: str = ESTADO) -> dict[str, Any]:
    try:
        with open(caminho) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def gravar_estado(ids: list[int], quando: float, caminho: str = ESTADO) -> None:
    os.makedirs(os.path.dirname(caminho) or ".", exist_ok=True)
    tmp = caminho + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"avisado_em": quando, "ids": sorted(ids)}, f)
    os.replace(tmp, caminho)


def deve_avisar(ids: list[int], estado: dict[str, Any], agora_ts: float,
                cooldown_h: float = COOLDOWN_H) -> bool:
    """Avisa se há lead que ainda não foi avisado, ou se o cooldown venceu."""
    if not ids:
        return False
    ja = set(estado.get("ids") or [])
    if set(ids) - ja:
        return True
    ultimo = float(estado.get("avisado_em") or 0)
    return (agora_ts - ultimo) >= cooldown_h * 3600


def _enviar_padrao(texto: str) -> bool:
    from app.tools.telegram import TelegramTool

    # `send_to_dona` não levanta: devolve "ok", "ok (dry_run)" ou "error: …".
    # Só "ok" é aviso entregue — dry-run não conta, senão um teste calaria o
    # vigia de produção pelo cooldown inteiro.
    return TelegramTool().send_to_dona(texto) == "ok"


def rodar(session: Any, *, agora: datetime | None = None,
          enviar: Callable[[str], bool] = _enviar_padrao,
          caminho_estado: str = ESTADO, forcar_horario: bool = False) -> dict[str, Any]:
    """Uma passada. Devolve o que viu e o que fez (o script imprime)."""
    agora = agora or datetime.now(timezone.utc)
    if not forcar_horario and not no_horario(agora):
        return {"status": "fora_do_horario"}
    leads = procurar(session, agora)
    ids = [l.id for l in leads]
    if not leads:
        return {"status": "ok", "mudos": 0}
    estado = ler_estado(caminho_estado)
    if not deve_avisar(ids, estado, agora.timestamp()):
        return {"status": "cooldown", "mudos": len(leads), "ids": ids}
    texto = como_texto(leads, agora)
    if enviar(texto):
        gravar_estado(ids, agora.timestamp(), caminho_estado)
        log.warning("vigia_silencio_avisou", mudos=len(leads), ids=ids[:10])
        return {"status": "avisado", "mudos": len(leads), "ids": ids, "texto": texto}
    log.error("vigia_silencio_aviso_nao_saiu", mudos=len(leads))
    return {"status": "falha_no_aviso", "mudos": len(leads), "ids": ids, "texto": texto}
