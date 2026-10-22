"""Métricas de adoção do Copiloto — quanto das oportunidades geradas viraram ação.

Usado pelo briefing 20h da Dona pra mostrar se o time tá consumindo as análises.
Cobre janela rolling 7d.

Definições:
  gap_gerado     = lead_analyses com has_gap=true
  acao_marcada   = oportunidade_cuidada (corretora clicou "Já cuidei" no painel)

Taxa = acao_marcada / gap_gerado por corretora.
"""
from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import text

from app.db.session import SessionLocal

log = structlog.get_logger()


def metrica_adocao_semana() -> dict[str, Any]:
    """
    Retorna métrica de adoção dos últimos 7 dias.

    Returns:
        {
          "gaps_gerados": int,            # análises com has_gap=true em 7d
          "acoes_marcadas": int,          # oportunidade_cuidada criadas em 7d
          "taxa_pct": float,              # 0-100
          "por_corretora": [
            {"corretora_id": int, "nome": str,
             "gaps": int, "acoes": int, "taxa_pct": float}
          ],
          "top_corretora": {"nome": str, "taxa_pct": float, "acoes": int, "gaps": int} | None,
        }
    """
    with SessionLocal() as s:
        # Totais globais 7d
        row = s.execute(text("""
            SELECT
              COUNT(DISTINCT lead_id) FILTER (WHERE has_gap=true) AS gaps,
              (SELECT COUNT(*) FROM oportunidade_cuidada
                WHERE marcado_em >= NOW() - INTERVAL '7 days') AS acoes
            FROM lead_analyses
            WHERE analyzed_at >= NOW() - INTERVAL '7 days'
        """)).first()
        gaps_total = int(row[0] or 0)
        acoes_total = int(row[1] or 0)

        # Por corretora: gaps gerados E ações marcadas (LEFT JOIN ambos lados)
        rows = s.execute(text("""
            WITH gaps_cor AS (
                SELECT corretora_id, COUNT(DISTINCT lead_id) AS gaps
                FROM lead_analyses
                WHERE analyzed_at >= NOW() - INTERVAL '7 days'
                  AND has_gap = true
                  AND corretora_id IS NOT NULL
                GROUP BY corretora_id
            ),
            acoes_cor AS (
                SELECT corretora_id, COUNT(*) AS acoes
                FROM oportunidade_cuidada
                WHERE marcado_em >= NOW() - INTERVAL '7 days'
                GROUP BY corretora_id
            )
            SELECT c.id, c.nome,
                   COALESCE(g.gaps, 0)   AS gaps,
                   COALESCE(a.acoes, 0)  AS acoes
            FROM corretoras c
            LEFT JOIN gaps_cor  g ON g.corretora_id  = c.id
            LEFT JOIN acoes_cor a ON a.corretora_id  = c.id
            WHERE c.ativo = true
            ORDER BY c.id
        """)).fetchall()

    por_corretora: list[dict[str, Any]] = []
    for r in rows:
        gaps = int(r[2] or 0)
        acoes = int(r[3] or 0)
        taxa = round(100.0 * acoes / gaps, 1) if gaps else 0.0
        por_corretora.append({
            "corretora_id": r[0],
            "nome": r[1],
            "gaps": gaps,
            "acoes": acoes,
            "taxa_pct": taxa,
        })

    # Top corretora: maior taxa entre as que tiveram gap (evita 100% de 0/0)
    candidatas = [p for p in por_corretora if p["gaps"] > 0]
    top = max(candidatas, key=lambda p: (p["taxa_pct"], p["acoes"])) if candidatas else None
    top_out = (
        {"nome": top["nome"], "taxa_pct": top["taxa_pct"],
         "acoes": top["acoes"], "gaps": top["gaps"]}
        if top else None
    )

    taxa_global = round(100.0 * acoes_total / gaps_total, 1) if gaps_total else 0.0

    log.info(
        "adocao_metrica_calculada",
        gaps=gaps_total, acoes=acoes_total, taxa_pct=taxa_global,
        top=top_out["nome"] if top_out else None,
    )

    return {
        "gaps_gerados": gaps_total,
        "acoes_marcadas": acoes_total,
        "taxa_pct": taxa_global,
        "por_corretora": por_corretora,
        "top_corretora": top_out,
    }


def oportunidades_pendentes(corretora_id: int) -> dict[str, int]:
    """
    Conta oportunidades pendentes (não cuidadas) da corretora no painel
    /atendimento, agrupadas por temperatura. Lê a análise MAIS RECENTE
    por lead (DISTINCT ON) e filtra:
      • status_em_atendimento OR entrou_por='em_atendimento'
      • sem registro em oportunidade_cuidada

    Returns:
        {"quentes": int, "mornos": int, "parados": int, "total": int}
    """
    STATUS_EM_ATENDIMENTO = "10000005"
    with SessionLocal() as s:
        rows = s.execute(text("""
            WITH ultima AS (
                SELECT DISTINCT ON (la.lead_id)
                    la.lead_id, la.corretora_id, la.temperatura,
                    la.raw_analysis
                FROM lead_analyses la
                WHERE la.corretora_id = :cid
                ORDER BY la.lead_id, la.analyzed_at DESC
            )
            SELECT
                u.temperatura,
                COUNT(*) AS n
            FROM ultima u
            LEFT JOIN leads_snapshot ls ON ls.lead_id = u.lead_id
            LEFT JOIN oportunidade_cuidada oc
                   ON oc.corretora_id = u.corretora_id
                  AND oc.lead_id = u.lead_id
            WHERE (ls.etapa_kommo = :status_em_atend
                   OR u.raw_analysis->>'entrou_por' = 'em_atendimento')
              AND oc.id IS NULL
            GROUP BY u.temperatura
        """), {"cid": corretora_id, "status_em_atend": STATUS_EM_ATENDIMENTO}).fetchall()

    counts = {"quentes": 0, "mornos": 0, "parados": 0}
    for temp, n in rows:
        if temp == "quente":
            counts["quentes"] += int(n)
        elif temp == "morno":
            counts["mornos"] += int(n)
        else:
            counts["parados"] += int(n)
    counts["total"] = sum(counts.values())
    return counts
