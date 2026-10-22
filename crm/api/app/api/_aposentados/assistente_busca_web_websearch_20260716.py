"""
Assistente — busca de imóvel de TERCEIROS na web (fora da carteira Acme).

Quando o lead quer um imóvel que NÃO está nos 114 empreendimentos próprios, a
corretora pede pra Assistente pesquisar na internet. A Assistente busca em portais BR
(ZAP, VivaReal, OLX, Chaves na Mão, imobiliárias locais) e devolve 4-6 opções
REAIS com LINK clicável pra corretora abrir e validar.

REGRA DE OURO: isto é SUGESTÃO pra corretora. NUNCA envia nada pro lead.

Motor: usa a ferramenta de WEB SEARCH server-side via `claude -p --allowedTools
WebSearch WebFetch` (Claude Code CLI, plano Max — sem custo de API, sem exigir
ANTHROPIC_API_KEY). Isso garante links REAIS (não inventados), que é o ponto.
"""
from __future__ import annotations

import json as _json
import os
import re
import subprocess
import time
from typing import Any

import structlog
from fastapi import APIRouter, Body, Header

from app.api.painel import _identificar_usuario, _viewer_info

log = structlog.get_logger()
router = APIRouter(prefix="/painel", tags=["assistente"])

# Busca web é lenta (WebSearch + WebFetch em vários portais). Timeout generoso.
_BUSCA_TIMEOUT_S = 240

_SYS = (
    "Você é a Assistente, assistente de corretoras da Acme Imóveis em Cidade/SP. "
    "A corretora te passou o pedido de um lead por um imóvel que NÃO está na carteira "
    "da Acme, então você vai BUSCAR NA WEB imóveis À VENDA que batam com o pedido, "
    "em portais BR (ZAP Imóveis, VivaReal, OLX, Chaves na Mão, Imovelweb, e sites de "
    "imobiliárias locais de Cidade e região). "
    "Use a ferramenta de busca na web pra achar anúncios REAIS e clique/abra pra pegar "
    "os dados. Devolva 4 a 6 opções REAIS, cada uma com um LINK que abre o anúncio. "
    "NUNCA invente anúncios nem links — se não achar link real, não inclua a opção."
)

# Instrução de saída: JSON puro no shape que o front consome.
_OUT_INSTR = (
    "Responda SOMENTE com um array JSON (nada antes, nada depois, sem markdown, sem ```). "
    "Cada item deve ter EXATAMENTE estas chaves:\n"
    '[{"titulo": str, "preco": str, "bairro": str, "cidade": str, "m2": str, '
    '"quartos": str, "portal": str, "url": str, "resumo": str}]\n'
    "- url: link REAL e completo do anúncio (https://...). Obrigatório.\n"
    "- preco: como aparece no anúncio (ex 'R$ 350.000'). Se não souber, null.\n"
    "- m2/quartos: string ou número; se não souber, null.\n"
    "- resumo: 1 frase curta com o diferencial do imóvel.\n"
    "Campo desconhecido = null. 4 a 6 itens. Se não achar NADA real, responda []."
)


def _run_web_search(texto: str) -> tuple[list[dict], str | None]:
    """Roda a busca web via claude CLI com WebSearch/WebFetch.

    Retorna (opcoes, erro). erro=None em sucesso.
    """
    prompt = (
        f"{_SYS}\n\n"
        f"PEDIDO DA CORRETORA: {texto}\n\n"
        f"{_OUT_INSTR}"
    )
    cmd = [
        "claude", "-p", prompt,
        "--allowedTools", "WebSearch", "WebFetch",
        "--output-format", "text",
    ]
    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=_BUSCA_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        log.warning("assistente_busca_web_timeout", timeout_s=_BUSCA_TIMEOUT_S)
        return [], "a busca demorou demais — tenta de novo em instantes"
    except Exception as e:  # noqa: BLE001
        log.warning("assistente_busca_web_subprocess_erro", erro=str(e)[:160])
        return [], "não consegui buscar agora, tenta de novo"

    duration_ms = int((time.monotonic() - start) * 1000)
    out = result.stdout or ""
    log.info(
        "assistente_busca_web_call",
        rc=result.returncode,
        duration_ms=duration_ms,
        input_chars=len(prompt),
        output_chars=len(out),
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[:200]
        log.warning("assistente_busca_web_rc_erro", rc=result.returncode, stderr=stderr)
        return [], "não consegui buscar agora, tenta de novo"

    opcoes = _parse_opcoes(out)
    if opcoes is None:
        log.warning("assistente_busca_web_sem_json", preview=out[:200])
        return [], "não entendi o resultado da busca — tenta de novo"
    return _normalizar(opcoes), None


def _parse_opcoes(text: str) -> list[dict] | None:
    """Extrai o primeiro array JSON bem-formado do texto (tolera ``` e prosa)."""
    # tenta o texto inteiro primeiro (caso venha JSON puro)
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    for candidate in (cleaned, text):
        try:
            v = _json.loads(candidate)
            if isinstance(v, list):
                return v
        except (_json.JSONDecodeError, TypeError):
            pass
    # varre por um array balanceado
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "[":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0 and start >= 0:
                frag = text[start : i + 1]
                try:
                    v = _json.loads(frag)
                    if isinstance(v, list):
                        return v
                except _json.JSONDecodeError:
                    start = -1
                    depth = 0
    return None


_CAMPOS = ("titulo", "preco", "bairro", "cidade", "m2", "quartos", "portal", "url", "resumo")


def _normalizar(itens: list) -> list[dict]:
    """Garante o shape; descarta itens sem URL http (link é o ponto)."""
    out: list[dict] = []
    for it in itens:
        if not isinstance(it, dict):
            continue
        url = str(it.get("url") or "").strip()
        if not url.startswith("http"):
            continue  # sem link real não serve
        card = {}
        for k in _CAMPOS:
            v = it.get(k)
            card[k] = (str(v).strip() if v not in (None, "", "null") else None)
        card["url"] = url
        out.append(card)
    return out[:6]


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
    SUGESTÃO pra corretora — nunca envia nada pro lead.
    """
    user = _identificar_usuario(x_painel_user)
    texto = (payload.get("texto") or "").strip()
    if len(texto) < 4:
        return {"opcoes": [], "erro": "me diz o que o lead procura — ex: 'apto 3 quartos no Bairro A até 500 mil'", "pedido": texto}

    t0 = time.monotonic()
    opcoes, erro = _run_web_search(texto)
    latencia_ms = int((time.monotonic() - t0) * 1000)

    _log_busca(
        corretora_id=_viewer_info(user).get("corretora_id"),
        usuario=user,
        texto=texto,
        qtd=len(opcoes),
        latencia_ms=latencia_ms,
    )
    return {"opcoes": opcoes, "erro": erro, "pedido": texto}
