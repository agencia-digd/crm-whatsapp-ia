"""Cota de MARKETING no disparo — decisão da Dona, 30/07/2026.

Até aqui a regra era absoluta: disparo só em UTILITY, nunca MARKETING. Ela nasceu
de dano real (13 templates da Pessoa10 reclassificados em 27/07 = 10x o custo e lista
queimada) e continua sendo o DEFAULT.

O que muda: a Dona liberou uma COTA — até N% dos disparos do dia, por linha,
podem sair em MARKETING. O resto continua UTILITY obrigatório.

Três travas que ficam de pé:
  1. `DISPARO_COTA_MARKETING_PCT` default 0 → sem configurar, nada muda. A cota
     é opt-in explícito, não um afrouxamento silencioso.
  2. MARKETING só é considerado quando o caller PEDE (`permitir_marketing=True`).
     Nenhuma automação existente passa a mandar MARKETING por acidente.
  3. UTILITY tem PREFERÊNCIA sempre. A cota é um teto pra quando o caller quer
     MARKETING, não uma fatia que precisa ser gasta.

Contagem: `mensagens_whatsapp.conteudo_metadata->>'categoria'`, gravado no envio
(ver `cloud_corretora_a.gravar`). Fica no mesmo lugar que a mensagem — sem tabela
nova e sem migration, e é auditável olhando a conversa.

⚠️ A contagem é por DIA LOCAL (America/Sao_Paulo) e por LINHA. Não é atômica: dois
ticks simultâneos podem ambos ver "19 de 20" e passar. A trava de tick
(`trava_tick`) cobre o mesmo motor; o excesso possível é de 1 mensagem, e o custo
disso é baixo perto do de não ter cota nenhuma. Se um dia isso virar problema, o
lugar certo é o serviço único de despacho (relatório de 29/07), não um patch aqui.
"""
from __future__ import annotations

import os
from typing import Any

import structlog
from sqlalchemy import text as sql_text

log = structlog.get_logger()

CATEGORIA_UTILITY = "UTILITY"
CATEGORIA_MARKETING = "MARKETING"

# Quantos % dos disparos do dia podem ser MARKETING. 0 = trava histórica.
_ENV_PCT = "DISPARO_COTA_MARKETING_PCT"

_SQL_CONTA_HOJE = """
    SELECT
      count(*) FILTER (WHERE conteudo_metadata->>'categoria' = :mkt) AS marketing,
      count(*) AS total
    FROM mensagens_whatsapp
    WHERE instance = :inst
      AND from_corretora IS TRUE
      AND tipo_mensagem = 'template'
      AND (timestamp_evolution AT TIME ZONE 'America/Sao_Paulo')::date
          = (now() AT TIME ZONE 'America/Sao_Paulo')::date
"""


def pct_configurado() -> int:
    """% da cota, saneado pra [0, 100]. Valor inválido → 0 (fail-closed)."""
    try:
        pct = int(float((os.getenv(_ENV_PCT) or "0").strip()))
    except (TypeError, ValueError):
        log.warning("cota_marketing_pct_invalido", valor=os.getenv(_ENV_PCT))
        return 0
    return max(0, min(100, pct))


def contagem_hoje(slug: str, session: Any) -> tuple[int, int]:
    """(marketing, total) de templates enviados hoje por esta linha.

    Usa `instance_do_slug` (mapa ESTÁTICO), não `instance_da_linha`: esta última
    passa pelo registry, que só conhece a linha se a env `CLOUD_LINHA_<SLUG>`
    estiver no processo. Com a env ausente ela devolvia None, a contagem virava
    zero e a cota LIBERAVA MARKETING — fail-open exatamente onde não pode.
    """
    from app.services import cloud_lines

    inst = cloud_lines.instance_do_slug(slug)
    if not inst:
        return (0, 0)
    row = session.execute(
        sql_text(_SQL_CONTA_HOJE), {"inst": inst, "mkt": CATEGORIA_MARKETING}
    ).first()
    if row is None:
        return (0, 0)
    return (int(row[0] or 0), int(row[1] or 0))


def pode_marketing(slug: str, *, cap_diario: int, session: Any) -> bool:
    """True se ainda cabe MARKETING na cota de hoje desta linha.

    A cota é sobre o CAP do dia (o teto de envio da linha), não sobre o que já
    saiu — senão o primeiro disparo do dia nunca poderia ser MARKETING (0 de 0).

    FAIL-CLOSED: pct 0, linha sem instance, ou erro na contagem → False.
    """
    pct = pct_configurado()
    if pct <= 0:
        return False
    teto = int(cap_diario) * pct // 100
    if teto <= 0:
        log.info("cota_marketing_teto_zero", slug=slug, cap=cap_diario, pct=pct)
        return False
    # Sem instance conhecida não há como CONTAR o que já saiu — e sem contagem
    # não se autoriza MARKETING.
    from app.services import cloud_lines

    if not cloud_lines.instance_do_slug(slug):
        log.warning("cota_marketing_slug_sem_instance", slug=slug)
        return False
    try:
        usados, _total = contagem_hoje(slug, session)
    except Exception as e:  # noqa: BLE001 — não sei contar ⇒ não libero
        log.warning("cota_marketing_contagem_falhou", slug=slug, erro=str(e)[:140])
        return False
    cabe = usados < teto
    log.info("cota_marketing_avaliada", slug=slug, usados=usados, teto=teto,
             pct=pct, cap=cap_diario, liberado=cabe)
    return cabe
