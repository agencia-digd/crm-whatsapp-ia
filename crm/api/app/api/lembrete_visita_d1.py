"""/painel/automacoes/lembrete-visita-d1 — a véspera da visita, medida e cobrada.

Três leituras, admin-only como o resto de /painel/automacoes:

  GET .../metricas    → por semana: quantas visitas tiveram lembrete, quantas a
                        corretora confirmou, remarcou, cancelou NA VÉSPERA e
                        quantas ficaram sem resposta. É o número que diz se
                        isto funcionou — o conselho de 19/09 pediu a queda dos
                        47,6% de cancelamento, e sem medição a resposta seria
                        "achei que melhorou".
  GET .../pendentes   → o que precisa de gente: silêncio carimbado na hora do
                        corte, resposta que o parser não decifrou, e remarcação
                        esperando data nova. Esta lista é o antídoto pro que
                        matou o `confirmar_domingo`: lá, quem não respondia
                        sumia; aqui, aparece.
  GET .../estado      → o interruptor (ligado/desligado) e a próxima janela de
                        envio, pro painel não precisar adivinhar.

Tudo leitura. LIGAR a automação não é endpoint: é gravar
`lembrete_visita_d1_ativo` em `config_operacional`, e quem faz isso é a Dona.
"""
from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Header, Query

from app.services import lembrete_visita_d1 as d1

log = structlog.get_logger()

router = APIRouter(prefix="/painel/automacoes/lembrete-visita-d1",
                   tags=["painel", "automacoes"])


def _admin(x_painel_user: str | None) -> None:
    """Mesma porta de /painel/automacoes — importada tarde pra não puxar o
    painel inteiro no import deste módulo."""
    from app.api.painel import _identificar_usuario, _require_admin

    _require_admin(_identificar_usuario(x_painel_user))


@router.get("/metricas")
def metricas(
    semanas: int = Query(8, ge=1, le=52),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    _admin(x_painel_user)
    return {"semanas": d1.metricas_semanais(semanas=semanas)}


@router.get("/pendentes")
def pendentes(
    dias: int = Query(7, ge=1, le=90),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    _admin(x_painel_user)
    return d1.pendencias(dias=dias)


@router.get("/estado")
def estado(
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    _admin(x_painel_user)
    return {
        "ativo": d1.ativo(),
        "chave": d1.CHAVE_ATIVO,
        "hora_lembrete": d1.HORA_LEMBRETE,
        "hora_limite": d1.HORA_LIMITE_LEMBRETE,
        "hora_corte": d1.HORA_CORTE,
        "na_janela_de_envio": d1.janela_de_envio(),
    }
