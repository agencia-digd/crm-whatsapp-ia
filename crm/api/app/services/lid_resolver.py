"""
Resolvedor `@lid → telefone real` — fallback best-effort no caminho de gravação.

Contexto: mensagens WhatsApp com `remote_jid = <id>@lid` são opacas — o local part
NÃO é telefone. O Evolution v2 às vezes manda o telefone real no payload do webhook
(`key.remoteJidAlt`); quando NÃO manda, a mensagem cai órfã (não casa com lead).

Este módulo dá um fallback: consulta o Postgres INTERNO do Evolution
(`evolution_api."Message".key->>'remoteJidAlt'`), que guarda o mapping do próprio
Baileys (mapping sólido, 1:1). Usado SÓ quando `extrair_telefone_lid(data)` falha.

- Leitura apenas (nunca escreve no Evolution DB).
- Cache em memória com TTL — não bate no DB a cada mensagem.
- Best-effort: qualquer erro → retorna None e loga; NUNCA levanta (não pode quebrar
  a gravação de mensagem).

O backfill histórico em massa é feito por `scripts/resolver_lid_telefone.py` (mesma
fonte). Aqui é só o caminho quente (mensagem nova).
"""
from __future__ import annotations

import time

import structlog
from sqlalchemy import create_engine, text as sql_text
from sqlalchemy.engine import Engine

from app.config import settings

log = structlog.get_logger()

_CACHE_TTL_SEG = 6 * 3600  # 6h — mapping lid→pn é estável
_cache: dict[str, tuple[float, str | None]] = {}
_engine: Engine | None = None


def _get_engine() -> Engine | None:
    global _engine
    if _engine is None:
        try:
            _engine = create_engine(
                settings.evolution_db_url,
                pool_pre_ping=True,
                pool_size=1,
                max_overflow=1,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("lid_resolver_engine_falhou", erro=str(e))
            return None
    return _engine


def _so_digitos(v: str | None) -> str | None:
    if not v:
        return None
    d = "".join(c for c in v if c.isdigit())
    return d or None


def resolver_lid_telefone(lid_jid: str) -> str | None:
    """Dado um JID `<id>@lid`, retorna o telefone real (só dígitos) ou None.

    Best-effort. Consulta o Evolution DB (Message.remoteJidAlt), com cache TTL.
    Nunca levanta.
    """
    if not lid_jid or not lid_jid.endswith("@lid"):
        return None
    lid = lid_jid.split("@", 1)[0]

    agora = time.monotonic()
    hit = _cache.get(lid)
    if hit and (agora - hit[0]) < _CACHE_TTL_SEG:
        return hit[1]

    telefone: str | None = None
    engine = _get_engine()
    if engine is not None:
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    sql_text(
                        """
                        SELECT split_part(key->>'remoteJidAlt','@',1) AS pn
                        FROM evolution_api."Message"
                        WHERE key->>'remoteJid' = :jid
                          AND key->>'remoteJidAlt' LIKE '%@s.whatsapp.net'
                          AND key->>'remoteJidAlt' <> ''
                        LIMIT 1
                        """
                    ),
                    {"jid": lid_jid},
                ).first()
            if row:
                telefone = _so_digitos(row[0])
        except Exception as e:  # noqa: BLE001
            log.warning("lid_resolver_query_falhou", jid_suffix=lid_jid[-8:], erro=str(e))
            # não cacheia falha transitória de DB
            return None

    _cache[lid] = (agora, telefone)
    if telefone:
        log.info("lid_resolvido_via_evolution_db", jid_suffix=lid_jid[-8:],
                 telefone_suffix=telefone[-4:])
    return telefone
