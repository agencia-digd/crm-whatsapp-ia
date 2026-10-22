"""
Fila de aprovação das sugestões de follow-up.

A IA escreve a variável {{2}} do template lendo a conversa real. Antes de sair
pro lead, um humano aprova no painel — decisão da Dona (29/07): "ninguém manda
pro cliente da Acme um texto que nenhuma pessoa leu".

Fluxo:
    gerar()      → cria item `pendente` (só quando a IA achou contexto real)
    aprovar()    → libera; aceita edição do texto
    rejeitar()   → descarta; a cadência usa o template de sempre
    texto_aprovado() → o que a cadência chama na hora de disparar
    marcar_usada()   → registra que saiu

⚠️ ESTE MÓDULO NÃO ENVIA NADA. Ele só decide QUAL TEXTO vai na variável. Quem
envia continua sendo a cadência, pelo mesmo template UTILITY aprovado de sempre.

TRAVA DE SEGURANÇA: `texto_aprovado()` é fail-safe — qualquer dúvida (sem
sugestão, não aprovada, expirada, texto vazio, erro de banco) devolve None, e
None significa "usa o template". O caminho de erro nunca inventa mensagem.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import text as sql_text

from app.db.session import SessionLocal
from app.inspector.shadow_cadencia import sanitizar_variavel

log = structlog.get_logger()

STATUS_SOMBRA = "sombra"
STATUS_PENDENTE = "pendente"
STATUS_APROVADA = "aprovada"
STATUS_REJEITADA = "rejeitada"
STATUS_USADA = "usada"
STATUS_EXPIRADA = "expirada"

STATUS_VALIDOS = {
    STATUS_SOMBRA, STATUS_PENDENTE, STATUS_APROVADA,
    STATUS_REJEITADA, STATUS_USADA, STATUS_EXPIRADA,
}

# Sugestão aprovada tem validade: o contexto que ela usa envelhece (o lead pode
# ter respondido, o imóvel pode ter saído). Passou disso, vira template.
VALIDADE_HORAS = 72

# Teto de tamanho da variável. A Dona: "não escrever muita coisa".
MAX_CHARS = 300


def _norm9(tel: str | None) -> str:
    return "".join(c for c in (tel or "") if c.isdigit())[-8:]


# ── escrita ──────────────────────────────────────────────────────────────────

def enfileirar(res: Any, tenant_id: int = 1) -> int | None:
    """Grava uma sugestão da IA na fila.

    Só entra como `pendente` quando a IA achou CONTEXTO REAL. Sem contexto, o
    template de hoje já é a resposta certa — não faz sentido pedir revisão
    humana pra confirmar o óbvio. Esses ficam como `sombra` (viram dado, não
    viram fila).
    """
    status = STATUS_PENDENTE if getattr(res, "usou_contexto", False) else STATUS_SOMBRA
    try:
        with SessionLocal() as s:
            row = s.execute(
                sql_text(
                    """
                    INSERT INTO shadow_cadencia
                        (tenant_id, corretora_id, lead_telefone, lead_nome, toque,
                         template_atual, sugestao_ia, usou_contexto, contexto_usado,
                         motivo, modelo, status)
                    VALUES
                        (:t, :c, :tel, :nome, :toque, :tpl, :sug, :usou, :ctx,
                         :mot, :mod, :st)
                    RETURNING id
                    """
                ),
                {
                    "t": tenant_id, "c": res.corretora_id, "tel": res.lead_telefone,
                    "nome": res.lead_nome, "toque": res.toque,
                    "tpl": res.template_atual, "sug": res.sugestao_ia,
                    "usou": res.usou_contexto, "ctx": res.contexto_usado,
                    "mot": res.motivo, "mod": res.modelo, "st": status,
                },
            ).first()
            s.commit()
            return int(row[0]) if row else None
    except Exception as e:  # noqa: BLE001
        log.warning("sugestao_enfileirar_falhou", erro=str(e)[:200])
        return None


def aprovar(sugestao_id: int, por: str, texto_editado: str | None = None) -> bool:
    """Aprova (opcionalmente editando). Só sai de `pendente`."""
    texto = sanitizar_variavel(texto_editado) if texto_editado else None
    if texto is not None and (not texto or len(texto) > MAX_CHARS):
        log.warning("sugestao_texto_editado_invalido", id=sugestao_id, tam=len(texto or ""))
        return False
    try:
        with SessionLocal() as s:
            res = s.execute(
                sql_text(
                    """
                    UPDATE shadow_cadencia
                       SET status = :ap,
                           texto_final = coalesce(:txt, sugestao_ia),
                           aprovado_por = :por,
                           aprovado_em = now()
                     WHERE id = :id AND status = :pend
                    """
                ),
                {"ap": STATUS_APROVADA, "txt": texto, "por": por,
                 "id": sugestao_id, "pend": STATUS_PENDENTE},
            )
            s.commit()
            ok = (res.rowcount or 0) > 0
    except Exception as e:  # noqa: BLE001
        log.warning("sugestao_aprovar_falhou", id=sugestao_id, erro=str(e)[:200])
        return False
    log.info("sugestao_aprovada", id=sugestao_id, por=por, editada=bool(texto), ok=ok)
    return ok


def rejeitar(sugestao_id: int, por: str) -> bool:
    """Rejeita — a cadência segue com o template de sempre."""
    try:
        with SessionLocal() as s:
            res = s.execute(
                sql_text(
                    """
                    UPDATE shadow_cadencia
                       SET status = :rej, aprovado_por = :por, aprovado_em = now()
                     WHERE id = :id AND status = :pend
                    """
                ),
                {"rej": STATUS_REJEITADA, "por": por,
                 "id": sugestao_id, "pend": STATUS_PENDENTE},
            )
            s.commit()
            ok = (res.rowcount or 0) > 0
    except Exception as e:  # noqa: BLE001
        log.warning("sugestao_rejeitar_falhou", id=sugestao_id, erro=str(e)[:200])
        return False
    log.info("sugestao_rejeitada", id=sugestao_id, por=por, ok=ok)
    return ok


# ── leitura (o que a cadência chama) ─────────────────────────────────────────

def texto_aprovado(telefone: str, toque: int) -> tuple[int, str] | None:
    """Texto liberado pra ESTE (telefone, toque), ou None → usa o template.

    FAIL-SAFE por desenho: qualquer situação fora do caminho feliz devolve None.
    Sem sugestão, não aprovada, vencida, texto vazio, banco fora do ar — tudo
    vira "manda o template". O erro nunca inventa mensagem pro lead.
    """
    suf = _norm9(telefone)
    if not suf:
        return None
    limite = datetime.now(timezone.utc) - timedelta(hours=VALIDADE_HORAS)
    try:
        with SessionLocal() as s:
            row = s.execute(
                sql_text(
                    """
                    SELECT id, texto_final
                      FROM shadow_cadencia
                     WHERE right(regexp_replace(coalesce(lead_telefone,''), '\\D', '', 'g'), 8) = :suf
                       AND toque = :toque
                       AND status = :ap
                       AND aprovado_em >= :limite
                       AND texto_final IS NOT NULL
                     ORDER BY aprovado_em DESC
                     LIMIT 1
                    """
                ),
                {"suf": suf, "toque": toque, "ap": STATUS_APROVADA, "limite": limite},
            ).first()
    except Exception as e:  # noqa: BLE001
        log.warning("sugestao_lookup_falhou", erro=str(e)[:200])
        return None

    if not row:
        return None
    sid, txt = int(row[0]), (row[1] or "").strip()
    if not txt or len(txt) > MAX_CHARS:
        log.warning("sugestao_texto_invalido_ignorada", id=sid, tam=len(txt))
        return None
    return sid, txt


def marcar_usada(sugestao_id: int) -> None:
    """Registra que a sugestão de fato saiu. Best-effort — não trava o envio."""
    try:
        with SessionLocal() as s:
            s.execute(
                sql_text(
                    "UPDATE shadow_cadencia SET status = :u, usada_em = now() WHERE id = :id"
                ),
                {"u": STATUS_USADA, "id": sugestao_id},
            )
            s.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("sugestao_marcar_usada_falhou", id=sugestao_id, erro=str(e)[:200])


def expirar_vencidas() -> int:
    """Marca como `expirada` o que ficou pendente além da validade."""
    limite = datetime.now(timezone.utc) - timedelta(hours=VALIDADE_HORAS)
    try:
        with SessionLocal() as s:
            res = s.execute(
                sql_text(
                    """
                    UPDATE shadow_cadencia SET status = :exp
                     WHERE status = :pend AND created_at < :limite
                    """
                ),
                {"exp": STATUS_EXPIRADA, "pend": STATUS_PENDENTE, "limite": limite},
            )
            s.commit()
            n = res.rowcount or 0
    except Exception as e:  # noqa: BLE001
        log.warning("sugestao_expirar_falhou", erro=str(e)[:200])
        return 0
    if n:
        log.info("sugestoes_expiradas", n=n)
    return n
