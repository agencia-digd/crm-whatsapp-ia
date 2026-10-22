"""Correções humanas voltam pro prompt do Aprendiz (flag APRENDIZ_CORRECOES_NO_PROMPT).

A corretora/Dona marca na tela /aprendiz o rascunho que errou (`acerto = false`).
Até aqui isso só mexia no placar. Com a flag ligada, as últimas correções entram
no prompt da sombra e do juiz como exemplos do que NÃO fazer.

Regras:
  * flag OFF (padrão) → bloco vazio: o prompt fica idêntico, byte a byte;
  * só correção HUMANA (`acerto = false`), nunca veredito de IA;
  * teto de 5 exemplos, os mais recentes, só do texto do rascunho;
  * nada de dado pessoal: telefone, e-mail, link e o nome do lead somem do
    texto; rascunho que ainda sobra com número longo é descartado inteiro;
  * qualquer falha de banco → bloco vazio (o Aprendiz nunca cai por isso).
"""
from __future__ import annotations

import os
import re
from typing import Any, Callable

import structlog
from sqlalchemy import text

log = structlog.get_logger()

TETO_CORRECOES = 5
_MAX_CHARS = 280

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_URL = re.compile(r"(https?://|www\.)\S+", re.I)
_NUMERO_LONGO = re.compile(r"\(?\d[\d\s().+-]{6,}\d")  # telefone, CPF, CEP, etc.
_DIGITOS = re.compile(r"\d")


def ligado() -> bool:
    return (os.getenv("APRENDIZ_CORRECOES_NO_PROMPT") or "").strip().lower() in ("1", "true", "sim", "on")


def limpar(rascunho: str | None, lead_nome: str | None) -> str | None:
    """Rascunho sem dado pessoal, ou None se não dá pra garantir que está limpo."""
    t = (rascunho or "").strip()
    if not t:
        return None
    t = _EMAIL.sub("[email]", t)
    t = _URL.sub("[link]", t)
    t = _NUMERO_LONGO.sub("[número]", t)
    for parte in re.split(r"\s+", (lead_nome or "").strip()):
        if len(parte) >= 3:
            t = re.sub(rf"\b{re.escape(parte)}\b", "[nome]", t, flags=re.I)
    if len(_DIGITOS.findall(t)) > 6:  # ainda cheio de número: não arrisca
        return None
    t = re.sub(r"\s+", " ", t)
    return t if len(t) <= _MAX_CHARS else t[: _MAX_CHARS - 1].rstrip() + "…"


def ultimas_correcoes(
    session_factory: Callable[[], Any] | None = None,
    corretora_id: int | None = None,
    limite: int = TETO_CORRECOES,
) -> list[str]:
    """Últimos rascunhos que um humano marcou como errados, já limpos. Mais recente primeiro."""
    limite = max(0, min(limite, TETO_CORRECOES))
    if limite == 0:
        return []
    if session_factory is None:
        from app.db.session import SessionLocal as session_factory  # noqa: N813
    sql = (
        "SELECT resposta_sugerida, lead_nome FROM shadow_decisions "
        "WHERE acerto = false AND resposta_sugerida IS NOT NULL "
        "AND (CAST(:cid AS INTEGER) IS NULL OR corretora_id = CAST(:cid AS INTEGER)) "
        "ORDER BY avaliado_em DESC NULLS LAST, id DESC LIMIT :n"
    )
    with session_factory() as s:
        rows = s.execute(text(sql), {"cid": corretora_id, "n": limite * 4}).mappings().all()
    saida: list[str] = []
    for r in rows:
        t = limpar(r["resposta_sugerida"], r["lead_nome"])
        if t and t not in saida:
            saida.append(t)
        if len(saida) >= limite:
            break
    return saida


def bloco_para_prompt(
    corretora_id: int | None = None,
    session_factory: Callable[[], Any] | None = None,
) -> str:
    """Texto a anexar ao system prompt. Vazio com a flag OFF, sem correção ou com erro."""
    if not ligado():
        return ""
    try:
        exemplos = ultimas_correcoes(session_factory, corretora_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("aprendiz_correcoes_falhou", erro=str(exc)[:200])
        return ""
    if not exemplos:
        return ""
    linhas = "\n".join(f"- {e}" for e in exemplos)
    return (
        "\n\nCORREÇÕES RECENTES — rascunhos que a corretora marcou como ERRADOS "
        "(não repita o jeito, a ação ou o tom):\n" + linhas
    )
