"""
Assistente — busca de imóvel de TERCEIROS na web (fora da carteira Acme).

Quando o lead quer um imóvel que NÃO está nos 114 empreendimentos próprios, a
corretora pede pra Assistente pesquisar na internet. A Assistente busca em portais BR
(VivaReal, Chaves na Mão) e devolve até 6 opções REAIS com LINK clicável pra a
corretora abrir e validar.

REGRA DE OURO: isto é SUGESTÃO pra corretora. NUNCA envia nada pro lead.

Motor (GRÁTIS): serviço Playwright/Chromium rodando NO MAC MINI (imovel-fetcher,
Vpn :11441). O copiloto só faz proxy pro Mac — sem API paga, sem
web_search. O Mac abre a busca estruturada dos portais, extrai os cards por
seletor e devolve JSON. Fonte da verdade do serviço:
    copiloto-acme/mac-imovel-fetcher/server.js

Histórico: a v1 usava `claude -p --allowedTools WebSearch WebFetch` (motor pago).
Aposentado em 2026-07-16 (Dona pediu GRÁTIS) → app/api/_aposentados/.
"""
from __future__ import annotations

import os
import time
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, Body, Header

from app.api.painel import _identificar_usuario, _viewer_info
from app.config import settings

log = structlog.get_logger()
router = APIRouter(prefix="/painel", tags=["assistente"])

# Playwright no Mac é lento (abre Chromium, espera cards em 2 portais). Timeout
# generoso mas finito — se o Mac estiver off, devolve erro claro.
_BUSCA_TIMEOUT_S = 90.0

_CAMPOS = ("titulo", "preco", "bairro", "cidade", "m2", "quartos", "portal", "url", "resumo")


def _chamar_mac(texto: str) -> tuple[list[dict], str | None]:
    """Chama o imovel-fetcher no Mac (Vpn). Retorna (opcoes, erro)."""
    url = f"{settings.imovel_fetcher_url.rstrip('/')}/buscar"
    start = time.monotonic()
    try:
        with httpx.Client(timeout=_BUSCA_TIMEOUT_S) as cli:
            resp = cli.get(url, params={"texto": texto})
    except httpx.TimeoutException:
        log.warning("assistente_busca_web_timeout", timeout_s=_BUSCA_TIMEOUT_S)
        return [], "a busca demorou demais — tenta de novo em instantes"
    except httpx.HTTPError as e:
        # Mac off / Vpn caiu / porta fechada
        log.warning("assistente_busca_web_mac_off", erro=str(e)[:160])
        return [], "não consegui buscar agora (motor de busca no Mac indisponível) — tenta de novo"

    duration_ms = int((time.monotonic() - start) * 1000)
    if resp.status_code != 200:
        log.warning("assistente_busca_web_status", status=resp.status_code, body=resp.text[:200])
        return [], "não consegui buscar agora, tenta de novo"

    try:
        data = resp.json()
    except ValueError:
        log.warning("assistente_busca_web_json_invalido", preview=resp.text[:200])
        return [], "não entendi o resultado da busca — tenta de novo"

    opcoes = _normalizar(data.get("opcoes") or [])
    log.info(
        "assistente_busca_web_call",
        duration_ms=duration_ms,
        filtros=data.get("filtros"),
        portais=data.get("portais"),
        qtd=len(opcoes),
    )
    return opcoes, None


def _normalizar(itens: list) -> list[dict]:
    """Garante o shape esperado pelo front; descarta itens sem URL http real."""
    out: list[dict] = []
    for it in itens:
        if not isinstance(it, dict):
            continue
        url = str(it.get("url") or "").strip()
        if not url.startswith("http"):
            continue  # sem link real não serve — link é o ponto
        card: dict[str, Any] = {}
        for k in _CAMPOS:
            v = it.get(k)
            card[k] = str(v).strip() if v not in (None, "", "null") else None
        # o Mac não devolve "resumo"; deriva um curto do que temos (portal + m²)
        if not card.get("resumo"):
            partes = []
            if card.get("m2"):
                partes.append(f"{card['m2']} m²")
            if card.get("quartos"):
                partes.append(f"{card['quartos']} quartos")
            card["resumo"] = " · ".join(partes) or None
        card["url"] = url
        out.append(card)
    return out[:12]


def _log_busca(*, corretora_id: int | None, usuario: str, texto: str,
               qtd: int, latencia_ms: int) -> None:
    """Loga o intent 'busca_imovel_web' em assistente_conversas (best-effort)."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        from app.db.models import AssistenteConversa
        from app.db.session import SessionLocal
        with SessionLocal() as s:
            s.add(AssistenteConversa(
                corretora_id=corretora_id,
                usuario=usuario,
                intent="busca_imovel_web",
                pergunta=(texto or "")[:2000] or None,
                resposta_resumo=f"{qtd} opções encontradas na web"[:300],
                acao_tipo="busca_imovel_web",
                acao_executada=qtd > 0,
                latencia_ms=latencia_ms,
            ))
            s.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("assistente_busca_web_log_falha", erro=str(e)[:160])


@router.post("/assistente/buscar-imovel")
def assistente_buscar_imovel(
    payload: dict = Body(...),
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Busca imóveis de terceiros na web pro pedido da corretora.

    Body: {texto}. Retorna {opcoes: [...], erro?, pedido}.
    Motor GRÁTIS: Playwright no Mac (Vpn). SUGESTÃO pra corretora — nunca
    envia nada pro lead.
    """
    user = _identificar_usuario(x_painel_user)
    texto = (payload.get("texto") or "").strip()
    if len(texto) < 4:
        return {
            "opcoes": [],
            "erro": "me diz o que o lead procura — ex: 'apto 3 quartos no Bairro A até 500 mil'",
            "pedido": texto,
        }

    t0 = time.monotonic()
    opcoes, erro = _chamar_mac(texto)
    latencia_ms = int((time.monotonic() - t0) * 1000)

    _log_busca(
        corretora_id=_viewer_info(user).get("corretora_id"),
        usuario=user,
        texto=texto,
        qtd=len(opcoes),
        latencia_ms=latencia_ms,
    )
    return {"opcoes": opcoes, "erro": erro, "pedido": texto}
