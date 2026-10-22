"""Saúde de uma linha da Cloud API — o "celular está vermelho?" da tela.

## Por que existe

A qualidade da linha (`quality_rating`) é o que decide se o remarketing
dispara: em RED a rampa zera o cap e nada sai, com o interruptor ligado ou
não. Até 05/10/2026 isso só aparecia como uma frase no motivo da rampa. A Dona
pediu pra ver na tela se o celular está vermelho — antes de ligar, não depois.

## O que devolve

```
cor        verde | amarelo | vermelho | desconhecida
qualidade  GREEN | YELLOW | RED | UNKNOWN | None
status     CONNECTED | FLAGGED | RESTRICTED | …     (estado da linha na Meta)
lido_em    ISO-8601 UTC da leitura que PRODUZIU estes números
velha      True quando a Meta não respondeu agora e isto é a última leitura boa
erro       por que a leitura de agora falhou (None se deu certo)
```

## As duas regras

1. **Número sem hora de leitura vira número velho tratado como verdade.**
   `lido_em` é sempre a hora em que a Meta respondeu aquilo — quando a consulta
   de agora falha, devolvemos a última leitura boa COM A HORA DELA e
   `velha=True`. Nunca carimbamos hora nova em dado antigo.
2. **Sem leitura não é verde.** Falhou e não há leitura anterior → cor
   `desconhecida`. A tela mostra "não consegui ler", nunca uma bolinha verde
   por omissão.

## Cache

5 minutos por linha: a tela recarrega o mapa a cada ação e não pode bater na
Graph a cada render (a cota do app Meta é compartilhada com o gestor de
tráfego e a api de disparos). Falha NÃO é cacheada pelos 5 minutos — só por
30s, pra uma queda de rede não deixar a tela cega até o cache vencer.

## O que isto NÃO faz

Só GET de leitura. Não envia, não muda nada na linha. E é separado do cache de
qualidade da rampa (`rampa._qualidade_meta`, 10 min) de propósito: aquele
decide volume, este informa gente — mexer no TTL de um não pode mudar o outro.

O token vai no cabeçalho `Authorization`, nunca na query string: URL com
`access_token=` acaba em mensagem de exceção e daí em log.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

import structlog

log = structlog.get_logger()

GRAPH = "https://graph.facebook.com/v21.0"
CAMPOS = "display_phone_number,verified_name,quality_rating,status"

TTL_OK_S = 300
TTL_ERRO_S = 30

_COR = {
    "GREEN": "verde",
    "YELLOW": "amarelo",
    "RED": "vermelho",
}
# Estados da linha que valem vermelho mesmo com quality_rating bom: a Meta
# restringiu ou sinalizou o número, e a rampa trata os dois como parada.
_STATUS_VERMELHO = {"FLAGGED", "RESTRICTED", "BANNED", "DISCONNECTED"}

_lock = threading.Lock()
# slug → (monotonic da última tentativa, leitura boa mais recente | None, erro | None)
_cache: dict[str, tuple[float, dict[str, Any] | None, str | None]] = {}


def cor_de(qualidade: str | None, status: str | None) -> str:
    if status and status.upper() in _STATUS_VERMELHO:
        return "vermelho"
    return _COR.get((qualidade or "").upper(), "desconhecida")


def _buscar(slug: str) -> dict[str, Any]:
    """Uma consulta à Graph. Levanta em qualquer falha."""
    import os

    import httpx

    from app.services import cloud_lines as cl

    # Suíte de testes não fala com a Meta (mesma trava do conftest pro envio):
    # quem testa este módulo troca `_buscar` inteiro.
    if os.environ.get("COPILOTO_UNDER_PYTEST") == "1":
        raise RuntimeError("leitura da Meta bloqueada na suíte de testes")

    linha = cl.line_by_slug(slug)
    if linha is None:
        raise RuntimeError(f"linha '{slug}' não está configurada neste ambiente")
    token = cl.token_for(linha)
    if not token:
        raise RuntimeError(f"sem token pra ler a linha '{slug}'")
    r = httpx.get(
        f"{GRAPH}/{linha.phone_id}",
        params={"fields": CAMPOS},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    corpo = r.json() or {}
    if r.status_code != 200 or "error" in corpo:
        msg = (corpo.get("error") or {}).get("message") or f"HTTP {r.status_code}"
        raise RuntimeError(f"a Meta recusou a leitura: {str(msg)[:120]}")
    qualidade = corpo.get("quality_rating")
    qualidade = str(qualidade).upper() if qualidade else None
    status = corpo.get("status")
    status = str(status).upper() if status else None
    return {
        "slug": slug,
        "phone_number_id": linha.phone_id,
        "numero": corpo.get("display_phone_number"),
        "nome": corpo.get("verified_name"),
        "qualidade": qualidade,
        "status": status,
        "cor": cor_de(qualidade, status),
        "lido_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def ler(slug: str, *, forcar: bool = False) -> dict[str, Any]:
    """Saúde da linha, com cache. Nunca levanta."""
    agora = time.monotonic()
    with _lock:
        hit = _cache.get(slug)
        if hit and not forcar:
            idade = agora - hit[0]
            ttl = TTL_ERRO_S if hit[2] else TTL_OK_S
            if idade < ttl:
                return _montar(slug, hit[1], hit[2])
        ultima_boa = hit[1] if hit else None
        try:
            leitura = _buscar(slug)
            _cache[slug] = (agora, leitura, None)
            return _montar(slug, leitura, None)
        except Exception as e:  # noqa: BLE001 — tela não pode cair por causa disto
            erro = str(e)[:160]
            log.warning("saude_linha_indisponivel", slug=slug, erro=erro)
            _cache[slug] = (agora, ultima_boa, erro)
            return _montar(slug, ultima_boa, erro)


def _montar(slug: str, leitura: dict[str, Any] | None, erro: str | None) -> dict[str, Any]:
    if leitura is None:
        return {
            "slug": slug, "phone_number_id": None, "numero": None, "nome": None,
            "qualidade": None, "status": None, "cor": "desconhecida",
            "lido_em": None, "velha": False, "erro": erro or "sem leitura",
        }
    return {**leitura, "velha": erro is not None, "erro": erro}


def esquecer() -> None:
    """Zera o cache (testes)."""
    with _lock:
        _cache.clear()
