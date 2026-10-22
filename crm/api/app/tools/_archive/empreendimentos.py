"""
⚠️ APOSENTADA 11/07/2026 (Fase 3 PLANO-EMPREENDIMENTOS) — via SSH→Mac redundante.
A via oficial do catálogo é HTTP :11440 via dash (GET :8014/api/empreendimentos),
consumida por app/api/assistente_copilota.py::_empreendimentos(). Só o agente Pessoa
(dormant) usava esta tool — o import dele aponta pra cá até a Pessoa ser migrada.

Tool: catálogo vivo de empreendimentos da Acme (planilha CONTROLE EMPREENDIMENTOS).
Lê AO VIVO via sessão do Chrome no Mac mini (equipe@agencia) — a planilha tem rótulo
anti-IA + sharing externo bloqueado, então só dá pra ler pela sessão autenticada do browser.

Script no Mac: /Users/assistente-mac/acme-empreendimentos/ler_catalogo.py
Fonte/ref: scripts/empreendimentos/ler_catalogo.py
"""
from __future__ import annotations

import json
import shlex
import subprocess

import structlog

log = structlog.get_logger()

_MAC = "contato@example.com"
_MAC_PY = "/Users/assistente-mac/corretor/venv/bin/python"
_MAC_SCRIPT = "/Users/assistente-mac/acme-empreendimentos/ler_catalogo.py"
_TIMEOUT = 60


def consultar_empreendimentos(regiao: str | None = None,
                              valor_max: int | None = None,
                              quartos: str | None = None) -> dict:
    """
    Filtra o catálogo vivo de empreendimentos da Acme.

    Args:
        regiao: região/bairro de Cidade (substring), ex "zona norte" ou "trujillo"
        valor_max: teto de preço "a partir de" em reais, ex 250000
        quartos: nº de quartos, ex "2"

    Returns:
        {"ok": True, "total": N, "itens": [{empreendimento, construtora, a_partir,
         entrega, bairro, decorado, link}...]} ou {"ok": False, "erro": ...}
    """
    params = {k: v for k, v in
              {"regiao": regiao, "valor_max": valor_max, "quartos": quartos}.items()
              if v is not None}
    remote = f"{_MAC_PY} {_MAC_SCRIPT} {shlex.quote(json.dumps(params, ensure_ascii=False))}"
    cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no", _MAC, remote]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "erro": "timeout ao ler catálogo"}
    for line in reversed([l for l in out.stdout.strip().splitlines() if l.strip()]):
        try:
            return json.loads(line)
        except Exception:
            continue
    return {"ok": False, "erro": "sem retorno do catálogo", "stderr": out.stderr[-200:]}
