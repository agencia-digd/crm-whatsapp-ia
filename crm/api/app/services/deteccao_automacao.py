"""
Detecção dinâmica de mensagens de automação (saudação inicial,
follow-up automático, etc) por corretora.

Critério: msg considerada automação se aparece em ≥ N leads
DIFERENTES nos últimos K dias. Repetição pro mesmo lead é
insistência manual, não automação.

Algoritmo:
1. Lista leads recentes da corretora (leads_snapshot, janela K dias).
2. Pra cada lead: fetch conversa (Evolution).
3. Filtra mensagens enviadas pela corretora (from='corretora').
4. Normaliza texto.
5. Agrupa por texto_normalizado, conta JIDs distintos.
6. Candidatos com leads_distintos >= min → upsert em
   mensagens_automacao.

is_automacao(corretora_id, texto) é o ponto de uso pelo coaching.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import structlog
from sqlalchemy import text as sql_text

from app.db.session import SessionLocal

log = structlog.get_logger()


# ── Normalização ─────────────────────────────────────────────────────────────

# Faixas Unicode dos blocos de emoji mais comuns
_EMOJI_RANGES = [
    (0x1F300, 0x1FAFF),
    (0x2600, 0x27BF),
    (0x1F1E6, 0x1F1FF),  # bandeiras
    (0xFE00, 0xFE0F),    # variation selectors
    (0x200D, 0x200D),    # ZWJ
]


def _is_emoji_char(ch: str) -> bool:
    cp = ord(ch)
    for lo, hi in _EMOJI_RANGES:
        if lo <= cp <= hi:
            return True
    return False


_PUNCT_TRAILING_RE = re.compile(r"[\.\!\?\,\;\:…]+$")
_WHITESPACE_RE = re.compile(r"\s+")


def normalizar_texto(texto: str) -> str:
    """
    Normaliza pra dedupe:
      - lowercase
      - tira emojis (faixas Unicode comuns)
      - remove pontuação final repetida (...!!! → fim)
      - colapsa whitespace múltiplo
      - strip
    """
    if texto is None:
        return ""
    s = "".join(ch for ch in texto if not _is_emoji_char(ch))
    # NFD + drop combining marks → strip acentos (á→a, ç→c, etc)
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = s.lower().strip()
    # tira pontuação final
    s = _PUNCT_TRAILING_RE.sub("", s)
    # colapsa whitespace
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s


# ── Cache em memória pra is_automacao ────────────────────────────────────────

_cache: dict[int, set[str]] = {}


def _carregar_ativas(corretora_id: int) -> set[str]:
    if corretora_id in _cache:
        return _cache[corretora_id]
    with SessionLocal() as s:
        rows = s.execute(
            sql_text(
                "SELECT texto_normalizado FROM mensagens_automacao "
                "WHERE corretora_id = :cid AND ativa = true"
            ),
            {"cid": corretora_id},
        ).all()
    ativos = {r[0] for r in rows}
    _cache[corretora_id] = ativos
    return ativos


def invalidar_cache(corretora_id: int | None = None) -> None:
    """Limpa cache (chamar após upsert ou em testes)."""
    if corretora_id is None:
        _cache.clear()
    else:
        _cache.pop(corretora_id, None)


def is_automacao(corretora_id: int, texto: str) -> bool:
    if not texto:
        return False
    norm = normalizar_texto(texto)
    if not norm:
        return False
    return norm in _carregar_ativas(corretora_id)


# ── Detecção ─────────────────────────────────────────────────────────────────


def _fetch_leads_corretora(
    corretora_id: int, janela_dias: int, limit: int = 100,
) -> list[dict[str, Any]]:
    """Pega os N leads mais recentes da corretora (cap pra controlar custo Evolution)."""
    with SessionLocal() as s:
        rows = s.execute(
            sql_text(
                "SELECT lead_id, telefone FROM leads_snapshot "
                "WHERE corretora_id = :cid AND telefone IS NOT NULL AND telefone <> '' "
                "  AND ultima_sync >= NOW() - make_interval(days => :d) "
                "ORDER BY ultima_sync DESC LIMIT :lim"
            ),
            {"cid": corretora_id, "d": janela_dias, "lim": limit},
        ).all()
    return [{"lead_id": r[0], "telefone": r[1]} for r in rows]


def _fetch_corretora_meta(corretora_id: int) -> dict[str, Any] | None:
    with SessionLocal() as s:
        row = s.execute(
            sql_text(
                "SELECT id, nome, evolution_instance FROM corretoras WHERE id = :cid"
            ),
            {"cid": corretora_id},
        ).first()
    if not row:
        return None
    return {"id": row[0], "nome": row[1], "instance": row[2]}


def detectar_automacoes(
    corretora_id: int,
    janela_dias: int = 30,
    min_leads_distintos: int = 3,
    evolution_tool: Any | None = None,
) -> list[dict[str, Any]]:
    """
    Roda detecção e devolve candidatos. Não grava — gravação é via
    upsert_automacoes.
    """
    meta = _fetch_corretora_meta(corretora_id)
    if not meta or not meta["instance"]:
        log.info("automacao_skip_sem_instance", corretora_id=corretora_id)
        return []

    if evolution_tool is None:
        from app.tools.evolution import EvolutionTool
        evolution_tool = EvolutionTool()

    leads = _fetch_leads_corretora(corretora_id, janela_dias)
    log.info("automacao_leads", corretora_id=corretora_id, n_leads=len(leads))

    # texto_norm → {leads: set[lead_id], ocorrencias, exemplo, primeira, ultima}
    grupos: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"leads": set(), "ocorrencias": 0,
                 "exemplo": "", "primeira": None, "ultima": None}
    )

    janela_horas = janela_dias * 24
    for lead in leads:
        try:
            msgs = evolution_tool.get_conversation(
                meta["instance"], lead["telefone"], hours_back=janela_horas
            )
        except Exception as e:
            log.warning("automacao_fetch_error", lead_id=lead["lead_id"], error=str(e))
            continue

        for m in msgs:
            if m.get("from") != "corretora":
                continue
            texto = (m.get("text") or "").strip()
            if not texto:
                continue
            norm = normalizar_texto(texto)
            if not norm or len(norm) < 3:
                continue

            g = grupos[norm]
            g["leads"].add(lead["lead_id"])
            g["ocorrencias"] += 1
            if not g["exemplo"]:
                g["exemplo"] = texto

            at = m.get("at") or ""
            if at:
                if g["primeira"] is None or at < g["primeira"]:
                    g["primeira"] = at
                if g["ultima"] is None or at > g["ultima"]:
                    g["ultima"] = at

    candidatos: list[dict[str, Any]] = []
    for norm, g in grupos.items():
        n_leads = len(g["leads"])
        if n_leads < min_leads_distintos:
            continue
        candidatos.append({
            "texto_normalizado": norm,
            "texto_exemplo": g["exemplo"],
            "ocorrencias": g["ocorrencias"],
            "leads_distintos": n_leads,
            "primeira_vez": g["primeira"],
            "ultima_vez": g["ultima"],
        })

    log.info("automacao_candidatos", corretora_id=corretora_id,
             total_grupos=len(grupos), candidatos=len(candidatos))
    return candidatos


def upsert_automacoes(corretora_id: int, candidatos: Iterable[dict[str, Any]]) -> int:
    """
    Upsert. Se a linha existe com ativa=false (Dona rejeitou), mantém false.
    Se ativa=true, atualiza ocorrencias/leads_distintos/ultima_vez.
    Retorna nº de linhas afetadas.
    """
    n = 0
    with SessionLocal() as s:
        for c in candidatos:
            params = {
                "cid": corretora_id,
                "norm": c["texto_normalizado"],
                "exemplo": c["texto_exemplo"],
                "oc": int(c["ocorrencias"]),
                "ld": int(c["leads_distintos"]),
                "pv": c["primeira_vez"] or datetime.now(timezone.utc),
                "uv": c["ultima_vez"] or datetime.now(timezone.utc),
            }
            res = s.execute(sql_text(
                """
                INSERT INTO mensagens_automacao
                  (corretora_id, texto_normalizado, texto_exemplo,
                   ocorrencias, leads_distintos, primeira_vez, ultima_vez)
                VALUES (:cid, :norm, :exemplo, :oc, :ld, :pv, :uv)
                ON CONFLICT (corretora_id, texto_normalizado) DO UPDATE SET
                  ocorrencias = EXCLUDED.ocorrencias,
                  leads_distintos = EXCLUDED.leads_distintos,
                  ultima_vez = EXCLUDED.ultima_vez,
                  texto_exemplo =
                    CASE WHEN mensagens_automacao.texto_exemplo = ''
                         THEN EXCLUDED.texto_exemplo
                         ELSE mensagens_automacao.texto_exemplo
                    END
                """
            ), params)
            n += res.rowcount or 0
        s.commit()
    invalidar_cache(corretora_id)
    return n
