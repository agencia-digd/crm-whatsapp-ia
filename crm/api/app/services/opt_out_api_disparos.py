"""Opt-out que mora no api-disparos — a fonte que o copiloto não via.

Achado de 07/10/2026 (`projetos/crm/NUTRICAO-RESPEITA-OPT-OUT.md`): o lead 7880
clicou "Parar mensagens" em 30/08. O api-disparos gravou (`contacts.opted_out`);
o copiloto só guardou a mensagem. A varredura de 16/09 levou o card pra "Não
perturbe", a corretora arrastou o card de volta em 25/09 e a proteção sumiu com
o arraste. O único registro que sobrou do pedido foi o de lá.

O que conta como da Acme lá dentro:

  * `tenant_id = 7` — o tenant da Acme desde o corte de 27/09;
  * contato listado em `revisao_contatos_acme` — os 1.289 telefones Acme
    que ficaram no tenant 0 no corte, porque o dono não pôde ser comprovado.
    Opt-out de quem está nessa lista vale pra Acme: pedido de parar ignorado
    é o erro caro, um marco a menos não custa nada.

Opt-out de outro cliente da agência (outro tenant, fora da lista) NÃO conta: a
pessoa pediu pra parar de receber DAQUELE remetente.

SÓ LEITURA: a sessão abre com `default_transaction_read_only=on`. A URL é lida
do arquivo no momento do uso e nunca vai pra log. Qualquer falha LEVANTA
`Indisponivel` — quem chama decide, e a nutrição decide segurar o marco.
"""
from __future__ import annotations

import re
from typing import Any

TENANT_ACME = 7

_SQL = """
    SELECT c.phone
      FROM contacts c
     WHERE c.opted_out IS TRUE
       AND (c.tenant_id = :tenant
            OR EXISTS (SELECT 1 FROM revisao_contatos_acme r
                        WHERE r.contact_id = c.id))
"""

_engine_cache: dict[str, Any] = {}


class Indisponivel(RuntimeError):
    """Não deu pra ler o opt-out do api-disparos. A mensagem nunca leva a URL."""


def _url() -> str:
    from app.config import settings
    from app.services import cloud_lines as cl

    env = cl._parse_env_file(settings.api_disparos_db_env_file)
    url = (env.get(settings.api_disparos_db_var) or "").strip()
    if not url:
        raise Indisponivel(
            f"URL do banco do api-disparos ausente ({settings.api_disparos_db_var} em "
            f"{settings.api_disparos_db_env_file})"
        )
    return url


def _engine(url: str):
    from sqlalchemy import create_engine

    eng = _engine_cache.get(url)
    if eng is None:
        eng = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=1,
            max_overflow=0,
            connect_args={
                "connect_timeout": 5,
                "options": "-c default_transaction_read_only=on -c statement_timeout=5000",
            },
        )
        _engine_cache[url] = eng
    return eng


def _buscar_telefones() -> list[str]:
    """Telefones com opt-out que valem pra Acme. Levanta `Indisponivel`."""
    from sqlalchemy import text

    url = _url()
    try:
        with _engine(url).connect() as c:
            return [r[0] for r in c.execute(text(_SQL), {"tenant": TENANT_ACME})]
    except Exception as e:  # noqa: BLE001
        # Só o tipo do erro: a mensagem do driver pode carregar a URL.
        raise Indisponivel(f"api-disparos: {type(e).__name__}") from None


def sufixos_bloqueados() -> set[str]:
    """Sufixo-8 de todo telefone com opt-out no api-disparos (Acme)."""
    out: set[str] = set()
    for tel in _buscar_telefones():
        suf = re.sub(r"\D", "", tel or "")[-8:]
        if len(suf) == 8:
            out.add(suf)
    return out
