"""Busca de imóvel na web — API assíncrona (job).

A corretora dispara a busca e a tela LIBERA na hora: o POST devolve um `job_id`
e o resultado chega por polling. Ela pode fechar o modal, atender um lead e
voltar depois — o job continua no servidor e o resultado está lá.

Rotas (prefixo /painel):
    POST /painel/busca-imovel            → {job_id, status, reaproveitado}
    GET  /painel/busca-imovel/{job_id}   → {job_id, status, opcoes, erro, ...}
    GET  /painel/busca-imovel            → últimos jobs do usuário (reabrir tela)

O motor continua sendo o imovel-fetcher no Mac mini (Playwright, GRÁTIS).
REGRA DE OURO: SUGESTÃO pra corretora. NUNCA envia nada pro lead.

O endpoint síncrono antigo (`/painel/assistente/buscar-imovel`) continua vivo em
app/api/assistente_busca_web.py pra não quebrar cliente velho.
"""
from __future__ import annotations

import os
from typing import Any

import structlog
from fastapi import APIRouter, Body, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.painel import _identificar_usuario, _viewer_info
from app.services import busca_imovel_jobs as jobs

log = structlog.get_logger()
router = APIRouter(prefix="/painel", tags=["busca-imovel"])

_MIN_TEXTO = 4
_DICA = "me diz o que o lead procura — ex: 'apto 3 quartos no Bairro A até 500 mil'"


class NovaBuscaIn(BaseModel):
    texto: str = Field(default="", max_length=2000)


class NovaBuscaOut(BaseModel):
    job_id: int | None = None
    status: str
    pedido: str
    erro: str | None = None
    reaproveitado: bool = False


@router.post("/busca-imovel", response_model=NovaBuscaOut)
def criar_busca(
    payload: NovaBuscaIn = Body(default_factory=NovaBuscaIn),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> NovaBuscaOut:
    """Abre um job de busca e devolve o id NA HORA (não espera o Mac)."""
    user = _identificar_usuario(x_painel_user)
    texto = (payload.texto or "").strip()
    if len(texto) < _MIN_TEXTO:
        return NovaBuscaOut(status="erro", pedido=texto, erro=_DICA)

    corretora_id = _viewer_info(user).get("corretora_id")
    job_id, reaproveitado = jobs.criar_job(
        usuario=user, corretora_id=corretora_id, texto=texto
    )
    if not reaproveitado:
        jobs.disparar_worker(job_id)
        _limpeza_oportunista()
    return NovaBuscaOut(
        job_id=job_id,
        status=jobs.STATUS_RODANDO,
        pedido=texto,
        reaproveitado=reaproveitado,
    )


@router.get("/busca-imovel/{job_id}")
def consultar_busca(
    job_id: int,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Polling da tela: status + opções quando prontas."""
    user = _identificar_usuario(x_painel_user)
    dados = jobs.ler_job(job_id, usuario=user)
    if dados is None:
        raise HTTPException(status_code=404, detail="busca não encontrada")
    return dados


@router.get("/busca-imovel")
def listar_buscas(
    limite: int = Query(default=5, ge=1, le=20),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Últimas buscas do usuário — a tela reabre já mostrando o que ficou pronto."""
    user = _identificar_usuario(x_painel_user)
    return {"jobs": jobs.listar_jobs(usuario=user, limite=limite)}


def _limpeza_oportunista() -> None:
    """Retenção de 7 dias, sem cron: melhor esforço a cada busca nova."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        apagados = jobs.limpar_antigos()
        if apagados:
            log.info("busca_imovel_jobs_limpeza", apagados=apagados)
    except Exception as e:  # noqa: BLE001
        log.warning("busca_imovel_jobs_limpeza_falha", erro=str(e)[:160])
