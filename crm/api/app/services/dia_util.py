"""Janela de dia útil dos motores automáticos (porte da DEX, 02/10/2026).

De onde veio: `quandoPodeSair` do follow-up da DEX (`projetos/crm-dex/lib/fup.ts`)
— o que cai fora da janela espera o próximo dia útil. Na Acme o timer da
cadência rodava todo dia, e o banco mostrou envio no sábado 26/09 e no domingo
27/09.

O que cobre: cadência (follow-up de 8 toques), nutrição (marcos 30/60/90…150) e
mensagem planejada. A boas-vindas fica de FORA de propósito — o horário dela é
decisão da Dona (documento `reunioes/2026-10-02-motores-dex-para-acme.md`, §0.2).

Por que não perde mensagem: os três ticks selecionam o que está DEVIDO
(`proximo_toque_em <= agora` / `data_tarefa + hora <= now()`), e nada disso
expira. Segurar no fim de semana só adia: o primeiro tick de segunda pega tudo,
sob os mesmos caps de sempre. A cadência reespaça o próximo toque a partir do
envio real (`cadencia._avancar`), então dois toques não colam.

Chave: `config_operacional.envio_dias_uteis` (painel > env `ENVIO_DIAS_UTEIS` >
True). Erro lendo a chave = trava LIGADA (segurar é o lado seguro).
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import structlog

log = structlog.get_logger()

TZ_SP = ZoneInfo("America/Sao_Paulo")


def e_dia_util(agora: datetime | None = None) -> bool:
    """Segunda a sexta, no dia de São Paulo (não no dia UTC)."""
    agora = agora or datetime.now(timezone.utc)
    if agora.tzinfo is None:
        agora = agora.replace(tzinfo=timezone.utc)
    return agora.astimezone(TZ_SP).weekday() < 5


def trava_ligada() -> bool:
    try:
        from app.services import config_operacional

        return config_operacional.envio_dias_uteis()
    except Exception as e:  # noqa: BLE001 — na dúvida, segura
        log.warning("dia_util_config_indisponivel", erro=str(e)[:140])
        return True


def pode_enviar(agora: datetime | None = None, *, motor: str = "") -> bool:
    """True se o motor pode mandar agora. Loga quando segura."""
    if not trava_ligada():
        return True
    if e_dia_util(agora):
        return True
    log.info("envio_segurado_fim_de_semana", motor=motor)
    return False
