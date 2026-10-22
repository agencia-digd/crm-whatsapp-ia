"""
API do APRENDIZ — placar do modo sombra (o que a IA responderia, sem enviar).

Lê shadow_decisions e agrega por bucket:
    responderia_sozinha | rascunharia | chamaria_voce

Também expõe o "BATEU OU NÃO": compara o rascunho da IA com a resposta REAL
que a corretora mandou na sequência (mensagens_whatsapp) usando heurística
LOCAL de similaridade — ver app/services/comparador_sombra.py. Zero LLM.

Privacidade: corretora vê só o placar DELA. Admin (dona/pessoa/assistente) vê todas e
pode filtrar por ?corretora_id. Reutiliza a resolução de viewer do painel.

NUNCA envia nada — é só leitura/agregação.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text

from app.api.painel import _identificar_usuario, _is_admin, _viewer_info
from app.db.session import SessionLocal
from app.services.aprendiz_correcoes import ligado as _correcoes_ligado
from app.services.comparador_sombra import (
    JANELA_RESPOSTA_HORAS,
    THRESHOLD_BATEU,
    THRESHOLD_PARCIAL,
    VEREDITOS,
    VEREDITOS_FINAIS,
    agregar_placar,
    forma,
    veredito_final,
    montar_pares,
)

log = structlog.get_logger()

router = APIRouter(prefix="/aprendiz", tags=["aprendiz"])

_BUCKETS = ("responderia_sozinha", "rascunharia", "chamaria_voce")


def _escopo(x_painel_user: str | None, corretora_id: int | None) -> int | None:
    """Resolve o corretora_id efetivo. Corretora → forçado o dela. Admin → o pedido (ou None=todas)."""
    user = _identificar_usuario(x_painel_user)
    viewer = _viewer_info(user)
    my_cid = viewer["corretora_id"]
    is_admin = _is_admin(user)
    if my_cid is None and not is_admin:
        raise HTTPException(403, "painel_user_sem_corretora")
    if not is_admin:
        return my_cid          # corretora: sempre só o dela, ignora o filtro pedido
    return corretora_id        # admin: o que pediu (None = todas)


@router.get("/placar")
def placar(
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
    corretora_id: int | None = Query(default=None),
    dias: int = Query(default=30),
) -> dict[str, Any]:
    """Placar agregado: quantas a IA responderia sozinha / rascunharia / chapessoa você."""
    cid = _escopo(x_painel_user, corretora_id)

    # Última decisão por lead (DISTINCT ON) — não contar o mesmo lead 2x se rodou
    # o shadow mais de uma vez na janela.
    sql = """
        WITH ult AS (
            SELECT DISTINCT ON (corretora_id, coalesce(lead_id, lead_telefone))
                   corretora_id, bucket, confianca
            FROM shadow_decisions
            WHERE created_at >= now() - (:dias || ' days')::interval
              AND (CAST(:cid AS INT) IS NULL OR corretora_id = CAST(:cid AS INT))
            ORDER BY corretora_id, coalesce(lead_id, lead_telefone), created_at DESC
        )
        SELECT bucket, count(*) AS n, round(avg(confianca)) AS conf_media
        FROM ult GROUP BY bucket
    """
    with SessionLocal() as s:
        rows = s.execute(text(sql), {"cid": cid, "dias": dias}).mappings().all()

    por_bucket = {b: {"n": 0, "conf_media": None} for b in _BUCKETS}
    total = 0
    for r in rows:
        if r["bucket"] in por_bucket:
            por_bucket[r["bucket"]] = {"n": int(r["n"]), "conf_media": int(r["conf_media"]) if r["conf_media"] is not None else None}
            total += int(r["n"])

    autonomia = round(100 * por_bucket["responderia_sozinha"]["n"] / total) if total else 0
    return {
        "escopo": "corretora" if cid is not None else "todas",
        "corretora_id": cid,
        "dias": dias,
        "total": total,
        "buckets": por_bucket,
        "autonomia_pct": autonomia,  # % que a IA já responderia sozinha
    }


@router.get("/lead")
def lead(
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
    lead_id: str | None = Query(default=None),
    telefone: str | None = Query(default=None),
) -> dict[str, Any]:
    """Última decisão sombra de UM lead (pro botão Aprendiz dentro da conversa).

    Casa por lead_id (kommoLeadId) e, se não achar, pelos últimos 8 dígitos do telefone.
    Respeita o escopo da corretora (admin vê qualquer um).
    """
    cid = _escopo(x_painel_user, None)  # corretora → força a dela; admin → None (todas)
    if not lead_id and not telefone:
        raise HTTPException(400, "informe lead_id ou telefone")

    sql = """
        SELECT id, corretora_id, lead_nome, lead_telefone, bucket, confianca,
               resposta_sugerida, motivo, ultima_msg_lead, ultima_msg_at, created_at
        FROM shadow_decisions
        WHERE (CAST(:cid AS INT) IS NULL OR corretora_id = CAST(:cid AS INT))
          AND (
                (CAST(:lead_id AS TEXT) IS NOT NULL AND lead_id = CAST(:lead_id AS TEXT))
             OR (CAST(:suf AS TEXT) IS NOT NULL
                 AND right(regexp_replace(coalesce(lead_telefone,''), '\\D', '', 'g'), 8) = CAST(:suf AS TEXT))
          )
        ORDER BY created_at DESC
        LIMIT 1
    """
    suf = "".join(c for c in (telefone or "") if c.isdigit())[-8:] or None
    with SessionLocal() as s:
        r = s.execute(text(sql), {"cid": cid, "lead_id": lead_id, "suf": suf}).mappings().first()
    if not r:
        return {"encontrada": False}
    d = dict(r)
    for k in ("ultima_msg_at", "created_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    return {"encontrada": True, "decisao": d}


@router.get("/decisoes")
def decisoes(
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
    corretora_id: int | None = Query(default=None),
    bucket: str | None = Query(default=None),
    dias: int = Query(default=30),
    limit: int = Query(default=50, le=200),
) -> dict[str, Any]:
    """Lista as decisões (pra inspecionar/auditar). Filtra por bucket opcional."""
    cid = _escopo(x_painel_user, corretora_id)
    if bucket is not None and bucket not in _BUCKETS:
        raise HTTPException(400, "bucket_invalido")

    sql = """
        SELECT DISTINCT ON (corretora_id, coalesce(lead_id, lead_telefone))
               id, corretora_id, lead_nome, lead_telefone, bucket, confianca,
               resposta_sugerida, motivo, ultima_msg_lead, ultima_msg_at, created_at
        FROM shadow_decisions
        WHERE created_at >= now() - (:dias || ' days')::interval
          AND (CAST(:cid AS INT) IS NULL OR corretora_id = CAST(:cid AS INT))
          AND (CAST(:bucket AS TEXT) IS NULL OR bucket = CAST(:bucket AS TEXT))
        ORDER BY corretora_id, coalesce(lead_id, lead_telefone), created_at DESC
    """
    with SessionLocal() as s:
        rows = s.execute(
            text(sql), {"cid": cid, "bucket": bucket, "dias": dias}
        ).mappings().all()

    items = [dict(r) for r in rows]
    # ordena por confiança desc dentro do recorte e corta no limit
    items.sort(key=lambda x: (x["bucket"] != "responderia_sozinha", -(x["confianca"] or 0)))
    for it in items:
        for k in ("ultima_msg_at", "created_at"):
            if it.get(k) is not None:
                it[k] = it[k].isoformat()
    return {"total": len(items), "itens": items[:limit]}


# ── BATEU OU NÃO — rascunho da IA × resposta real da corretora ───────────────
#
# Computa on-the-fly (volume pequeno: ~centenas de decisões na janela).
# SQL propositalmente simples/portável (sem ::interval, DISTINCT ON, regexp) —
# o cruzamento por sufixo de telefone é feito em Python no comparador, o que
# também deixa tudo testável em SQLite.


def _carregar_pares(cid: int | None, dias: int) -> list[dict[str, Any]]:
    """Busca decisões com rascunho + mensagens reais da corretora e cruza."""
    agora = datetime.now(timezone.utc)
    cutoff = agora - timedelta(days=dias)

    sql_dec = """
        SELECT id, corretora_id, lead_id, lead_nome, lead_telefone, bucket,
               confianca, resposta_sugerida, ultima_msg_lead, ultima_msg_at,
               created_at, acerto, avaliado_por, juiz_veredito, juiz_motivo
        FROM shadow_decisions
        WHERE resposta_sugerida IS NOT NULL
          AND created_at >= :cutoff
          AND (CAST(:cid AS INTEGER) IS NULL OR corretora_id = CAST(:cid AS INTEGER))
        ORDER BY created_at DESC
    """
    # mensagens reais: da âncora mais antiga possível até agora (a janela de
    # resposta por decisão é aplicada no comparador).
    sql_msg = """
        SELECT corretora_id, contato_telefone, conteudo_texto, transcricao,
               timestamp_evolution
        FROM mensagens_whatsapp
        WHERE from_corretora = :vero
          AND timestamp_evolution >= :ts_min
          AND (CAST(:cid AS INTEGER) IS NULL OR corretora_id = CAST(:cid AS INTEGER))
    """
    with SessionLocal() as s:
        decisoes = [dict(r) for r in s.execute(
            text(sql_dec), {"cutoff": cutoff, "cid": cid}
        ).mappings().all()]
        if not decisoes:
            return []
        msgs = [dict(r) for r in s.execute(
            text(sql_msg), {"ts_min": cutoff, "cid": cid, "vero": True}
        ).mappings().all()]

    return montar_pares(decisoes, msgs)


def _nomes_corretoras() -> dict[int, str]:
    with SessionLocal() as s:
        rows = s.execute(text("SELECT id, nome FROM corretoras")).all()
    return {int(r[0]): r[1] for r in rows}


@router.get("/bateu/placar")
def bateu_placar(
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
    corretora_id: int | None = Query(default=None),
    dias: int = Query(default=30, ge=1, le=180),
) -> dict[str, Any]:
    """Placar agregado do bateu-ou-não: % bateu geral e por corretora."""
    cid = _escopo(x_painel_user, corretora_id)
    pares = _carregar_pares(cid, dias)
    nomes = _nomes_corretoras()

    por_cid: dict[int | None, list[dict[str, Any]]] = {}
    for p in pares:
        por_cid.setdefault(p["corretora_id"], []).append(p)

    por_corretora = []
    for c, lista in sorted(por_cid.items(), key=lambda kv: (kv[0] is None, kv[0])):
        placar_c = agregar_placar(lista)
        placar_c["corretora_id"] = c
        placar_c["corretora_nome"] = nomes.get(c) if c is not None else None
        por_corretora.append(placar_c)

    geral = agregar_placar(pares)
    return {
        "escopo": "corretora" if cid is not None else "todas",
        "corretora_id": cid,
        "dias": dias,
        "janela_resposta_horas": JANELA_RESPOSTA_HORAS,
        "thresholds": {"bateu": THRESHOLD_BATEU, "parcial": THRESHOLD_PARCIAL},
        **geral,
        "por_corretora": por_corretora,
    }


@router.get("/bateu/pares")
def bateu_pares(
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
    corretora_id: int | None = Query(default=None),
    veredito: str | None = Query(default=None),
    final: str | None = Query(default=None),
    dias: int = Query(default=30, ge=1, le=180),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Lista paginada dos pares rascunho × resposta real, mais recentes primeiro."""
    cid = _escopo(x_painel_user, corretora_id)
    if veredito is not None and veredito not in VEREDITOS:
        raise HTTPException(400, "veredito_invalido")
    if final is not None and final not in VEREDITOS_FINAIS:
        raise HTTPException(400, "final_invalido")

    pares = _carregar_pares(cid, dias)
    if veredito is not None:
        pares = [p for p in pares if p["veredito"] == veredito]
    if final is not None:
        pares = [p for p in pares if p["veredito_final"] == final]
    total = len(pares)
    nomes = _nomes_corretoras()

    pagina = pares[offset : offset + limit]
    for p in pagina:
        p["corretora_nome"] = nomes.get(p["corretora_id"])
        if p["similaridade"] is not None:
            p["similaridade_pct"] = round(100 * p["similaridade"])
        else:
            p["similaridade_pct"] = None
        for k in ("rascunho_at", "resposta_real_at"):
            if p.get(k) is not None:
                p[k] = p[k].isoformat()
    return {"total": total, "offset": offset, "limit": limit, "itens": pagina}



# ── Marcação humana: acertou / errou ─────────────────────────────────────────


class AcertoIn(BaseModel):
    decisao_id: int
    acerto: bool | None = None  # True=acertou, False=errou, None=desfaz a marcação


@router.post("/acerto")
def marcar_acerto(
    body: AcertoIn,
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """Corretora/Dona marca se o rascunho da IA serviria. Grava `acerto`.

    Corretora só marca as decisões DELA; admin marca qualquer uma. Vale mesmo sem
    resposta real. A marcação humana tem precedência sobre o juiz IA no placar.
    """
    user = _identificar_usuario(x_painel_user)
    cid = _escopo(x_painel_user, None)  # corretora → o id dela; admin → None
    with SessionLocal() as s:
        row = s.execute(
            text("SELECT corretora_id FROM shadow_decisions WHERE id = :i"),
            {"i": body.decisao_id},
        ).first()
        if row is None:
            raise HTTPException(404, "decisao_nao_encontrada")
        if cid is not None and row[0] != cid:
            raise HTTPException(403, "decisao_de_outra_corretora")
        agora = datetime.now(timezone.utc) if body.acerto is not None else None
        s.execute(
            text(
                "UPDATE shadow_decisions SET acerto = :a, avaliado_em = :em, "
                "avaliado_por = :por WHERE id = :i"
            ),
            {
                "a": body.acerto, "em": agora,
                "por": user.lower() if body.acerto is not None else None,
                "i": body.decisao_id,
            },
        )
        s.commit()
    log.info("aprendiz_acerto_marcado", decisao_id=body.decisao_id,
             acerto=body.acerto, por=user)
    return {"ok": True, "decisao_id": body.decisao_id, "acerto": body.acerto}



# ── O que já aprendeu / o que mudou ──────────────────────────────────────────

# Corte da regra do próximo passo (prompt do Aprendiz, 05/08/2026 17:30 UTC).
_CORTE_REGRA = datetime(2026, 8, 5, 17, 30, tzinfo=timezone.utc)

# O que mudou no Aprendiz, em ordem. Só entra aqui o que está em vigor no código.
_MUDANCAS = (
    ("2026-08-05", "Regra do próximo passo",
     "Todo rascunho termina com pergunta ou compromisso com prazo, nunca com despedida. "
     "Antes, ~20% dos rascunhos encerravam conversa viva; as corretoras encerram 3%."),
    ("2026-10-21", "Placar por intenção, não por palavras",
     "\"Acertou\" agora vem da corretora/Dona (botões na tela) ou de um juiz que compara a AÇÃO "
     "do rascunho com a da corretora. O trigrama virou só informação."),
    ("2026-10-21", "Para quando a cota do Claude acaba",
     "A rodada pára no primeiro limite em vez de repetir erro em cadeia; as conversas "
     "sem decisão voltam na rodada seguinte."),
)


def _mudanca_correcoes(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Mudança "correções no prompt" com o antes × depois do acerto.

    O corte é APRENDIZ_CORRECOES_DESDE (data ISO em que a flag foi ligada). Sem ela,
    a mudança aparece sem número — não inventa efeito."""
    desde_txt = (os.getenv("APRENDIZ_CORRECOES_DESDE") or "").strip()
    m: dict[str, Any] = {
        "data": desde_txt[:10] or "—",
        "titulo": "Correções voltam pro prompt",
        "descricao": "Os últimos rascunhos que a corretora marcou como errados (até 5, sem "
                     "nome nem telefone) entram no prompt como exemplos do que não repetir.",
    }
    try:
        corte = _as_utc_dt(datetime.fromisoformat(desde_txt))
    except ValueError:
        return m
    antes, depois = [0, 0], [0, 0]  # [bateu, avaliados]
    for r in rows:
        if not r["resposta_sugerida"]:
            continue
        fin, _ = veredito_final(r["acerto"], r["juiz_veredito"], tem_real=True)
        if fin not in ("bateu", "parcial", "nao_bateu"):
            continue
        alvo = depois if _as_utc_dt(r["created_at"]) >= corte else antes
        alvo[1] += 1
        alvo[0] += fin == "bateu"
    pct = lambda a, b: round(100 * a / b) if b else None  # noqa: E731
    m["efeito"] = {
        "metrica": "rascunhos que bateram (avaliados)",
        "antes_pct": pct(*antes), "antes_n": antes[1],
        "depois_pct": pct(*depois), "depois_n": depois[1],
    }
    return m


def _segunda(dt: datetime) -> str:
    d = _as_utc_dt(dt).date()
    return (d - timedelta(days=d.weekday())).isoformat()


def _as_utc_dt(dt: datetime | str) -> datetime:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


@router.get("/aprendizado")
def aprendizado(
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
    corretora_id: int | None = Query(default=None),
    semanas: int = Query(default=8, ge=2, le=26),
) -> dict[str, Any]:
    """O que o Aprendiz já aprendeu e o que mudou: evolução por semana, regras em vigor
    com efeito medido e as últimas correções (rascunhos marcados como errados)."""
    cid = _escopo(x_painel_user, corretora_id)
    cutoff = datetime.now(timezone.utc) - timedelta(weeks=semanas)
    sql = """
        SELECT id, corretora_id, lead_nome, bucket, resposta_sugerida, created_at,
               acerto, avaliado_por, juiz_veredito, juiz_motivo
        FROM shadow_decisions
        WHERE created_at >= :cutoff
          AND (CAST(:cid AS INTEGER) IS NULL OR corretora_id = CAST(:cid AS INTEGER))
        ORDER BY created_at DESC
    """
    # regra do próximo passo: toda a história (antes × depois do corte), não só a janela
    sql_regra = """
        SELECT resposta_sugerida, created_at FROM shadow_decisions
        WHERE resposta_sugerida IS NOT NULL
          AND (CAST(:cid AS INTEGER) IS NULL OR corretora_id = CAST(:cid AS INTEGER))
    """
    with SessionLocal() as s:
        rows = [dict(r) for r in s.execute(text(sql), {"cutoff": cutoff, "cid": cid}).mappings().all()]
        regra_rows = [dict(r) for r in s.execute(text(sql_regra), {"cid": cid}).mappings().all()]

    semanas_map: dict[str, dict[str, int]] = {}
    for r in rows:
        w = semanas_map.setdefault(_segunda(r["created_at"]), {
            "decisoes": 0, "sozinha": 0, "rascunhos": 0, "despedidas": 0,
            "bateu": 0, "avaliados": 0,
        })
        w["decisoes"] += 1
        w["sozinha"] += r["bucket"] == "responderia_sozinha"
        if r["resposta_sugerida"]:
            w["rascunhos"] += 1
            w["despedidas"] += forma(r["resposta_sugerida"]) == "despedida"
            fin, _ = veredito_final(r["acerto"], r["juiz_veredito"], tem_real=True)
            if fin in ("bateu", "parcial", "nao_bateu"):
                w["avaliados"] += 1
                w["bateu"] += fin == "bateu"

    def pct(a: int, b: int) -> int | None:
        return round(100 * a / b) if b else None

    evolucao = [
        {
            "semana": k, "decisoes": v["decisoes"],
            "autonomia_pct": pct(v["sozinha"], v["decisoes"]),
            "despedida_pct": pct(v["despedidas"], v["rascunhos"]),
            "avaliados": v["avaliados"],
            "acertou_pct": pct(v["bateu"], v["avaliados"]),
        }
        for k, v in sorted(semanas_map.items())
    ]

    antes, depois = [0, 0], [0, 0]  # [despedidas, rascunhos]
    for r in regra_rows:
        alvo = depois if _as_utc_dt(r["created_at"]) >= _CORTE_REGRA else antes
        alvo[1] += 1
        alvo[0] += forma(r["resposta_sugerida"]) == "despedida"

    mudancas = [{"data": d, "titulo": t, "descricao": desc} for d, t, desc in _MUDANCAS]
    mudancas[0]["efeito"] = {
        "metrica": "rascunhos que se despedem",
        "antes_pct": pct(*antes), "antes_n": antes[1],
        "depois_pct": pct(*depois), "depois_n": depois[1],
    }

    if _correcoes_ligado():
        mudancas.append(_mudanca_correcoes(rows))

    correcoes = []
    for r in rows:  # já vem do mais recente pro mais antigo
        fin, fonte = veredito_final(r["acerto"], r["juiz_veredito"], tem_real=True)
        if fin == "nao_bateu" and r["resposta_sugerida"]:
            correcoes.append({
                "decisao_id": r["id"], "lead_nome": r["lead_nome"], "fonte": fonte,
                "rascunho": r["resposta_sugerida"], "motivo": r["juiz_motivo"],
                "quando": _as_utc_dt(r["created_at"]).isoformat(),
            })
        if len(correcoes) >= 8:
            break

    return {
        "escopo": "corretora" if cid is not None else "todas",
        "corretora_id": cid,
        "semanas": semanas,
        "evolucao": evolucao,
        "mudancas": mudancas,
        "correcoes": correcoes,
        # transparência: reflete a flag APRENDIZ_CORRECOES_NO_PROMPT (default OFF)
        "correcoes_alimentam_prompt": _correcoes_ligado(),
    }
