"""
Resume o funil semanal de um mês (Postgres-only — Sub-onda 10.1.d).

Lê vendas_mensais JOIN corretoras pra montar o quadro TOTAL GERAL +
corretoras × indicadores (Total/Atendeu/Agendou/Realizada/Vendas/VGV).

Agendou/Realizada vêm da tabela `visitas` (fonte canônica, mesma query que
o painel adotou em 21/05/2026) sempre que houver visita registrada no mês;
caindo pra vendas_mensais só nos meses históricos, anteriores à tabela.

Uso típico (subprocess via agent.py):
  python3 summary.py                    # mês corrente
  python3 summary.py --month 2026-04
  python3 summary.py --month 2026-05 --json

Saída: JSON em stdout. Erros vão pra stderr com exit !=0.
"""
import argparse
import json
import os
import sys
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg


SP_TZ = ZoneInfo("America/Sao_Paulo")

MONTH_PT = ["", "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
            "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]

INDICADORES = ["Total", "Atendeu", "Agendou", "Realizada", "Vendas", "VGV"]

# Cada chave é um degrau do funil e cada valor é o degrau imediatamente acima.
# São taxas ENCAIXADAS: o numerador é, por definição, subconjunto do
# denominador — quem realizou visita agendou, quem agendou foi atendido.
PCT_BASE = {"Atendeu": "Total", "Agendou": "Atendeu", "Realizada": "Agendou"}

SQL_VENDAS_MENSAIS = """
    SELECT c.id, c.nome,
           COALESCE(v.qtde_leads, 0)     AS total,
           COALESCE(v.qtde_atendidos, 0) AS atendeu,
           COALESCE(v.qtde_agendados, 0) AS agendou,
           COALESCE(v.qtde_visitas, 0)   AS realizada,
           COALESCE(v.qtde_vendas, 0)    AS vendas,
           COALESCE(v.vgv, 0)            AS vgv,
           v.fonte
      FROM corretoras c
      LEFT JOIN vendas_mensais v
        ON v.corretora_id = c.id AND v.ano_mes = %s
     WHERE c.ativo = TRUE
     ORDER BY c.id
"""

# Fonte canônica de Agendou/Realizada — mesmo par de FILTERs do painel
# (app/api/painel.py). "Agendada" é toda visita que não foi cancelada;
# "realizada" é só quem compareceu. Assim o subconjunto nunca escapa do
# conjunto e a taxa de comparecimento não estoura 100%.
SQL_VISITAS_MES = """
    SELECT corretora_id,
           COUNT(*) FILTER (WHERE status <> 'cancelada') AS agendados,
           COUNT(*) FILTER (WHERE status = 'compareceu') AS realizadas
      FROM visitas
     WHERE data_hora >= %s AND data_hora < %s
     GROUP BY corretora_id
"""


def _err(msg: str, code: int = 1):
    sys.stderr.write(msg + "\n")
    sys.exit(code)


def _parse_month(arg: str | None) -> tuple[int, int]:
    if not arg:
        now = datetime.now()
        return now.year, now.month
    try:
        y, m = map(int, arg.split("-"))
        if not (1 <= m <= 12):
            raise ValueError
        return y, m
    except (ValueError, IndexError):
        _err(f"--month inválido: {arg} (esperado YYYY-MM, ex: 2026-05)")


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    """[início, fim) do mês em America/Sao_Paulo — mesmo fuso do painel."""
    ini = datetime(year, month, 1, tzinfo=SP_TZ)
    if month == 12:
        fim = datetime(year + 1, 1, 1, tzinfo=SP_TZ)
    else:
        fim = datetime(year, month + 1, 1, tzinfo=SP_TZ)
    return ini, fim


def _pct(n: int, d: int) -> float | None:
    """Taxa de funil ENCAIXADO. Nunca retorna > 100%.

    Numerador maior que denominador não é uma taxa alta — é dado inconsistente
    (o subconjunto não pode passar do conjunto). Nesse caso devolve None, e
    quem chama sinaliza a inconsistência em vez de mostrar "233%" pra cliente.
    """
    if not d:
        return None
    if n > d:
        return None
    return round(n / d, 4)


def _pct_entry(n: int, d: int) -> dict:
    """Bloco {pct, inconsistente?} de um degrau do funil."""
    entry: dict = {"pct": _pct(n, d)}
    if d and n > d:
        entry["inconsistente"] = True
    return entry


def _with_pct(ind_map: dict) -> dict:
    out = {}
    for ind in INDICADORES:
        v = ind_map[ind]
        entry = {"valor": v}
        if ind in PCT_BASE:
            entry.update(_pct_entry(int(v), int(ind_map[PCT_BASE[ind]])))
        out[ind] = entry
    return out


def _inconsistencias(escopo: str, ind_map: dict) -> list[dict]:
    """Lista os degraus onde numerador > denominador (pct suprimido)."""
    achados = []
    for ind, base in PCT_BASE.items():
        n, d = int(ind_map[ind]), int(ind_map[base])
        if d and n > d:
            achados.append({
                "escopo": escopo,
                "indicador": ind,
                "base": base,
                "valor": n,
                "valor_base": d,
            })
    return achados


def _fmt_brl(v: Decimal | int | float) -> str:
    v = float(v or 0)
    return f"R$ {v:,.0f}".replace(",", ".")


def _fmt_pct(p: float | None) -> str:
    if p is None:
        return "—"
    return f"{p * 100:.1f}%"


def _build_payload(rows: list, visitas_por_corr: dict, ano_mes: str) -> dict:
    """Monta o JSON de saída. Puro — não toca no banco.

    rows: (id, nome, total, atendeu, agendou, realizada, vendas, vgv, fonte)
          vindas de vendas_mensais.
    visitas_por_corr: {corretora_id: (agendados, realizadas)} vindo da tabela
          `visitas`. Corretora ausente do dict cai no valor de vendas_mensais
          (meses históricos, anteriores à tabela `visitas`).
    """
    year, month = int(ano_mes[:4]), int(ano_mes[5:7])
    target_label = f"{MONTH_PT[month]} {year}"

    by_corr = []
    total_block: dict = {ind: 0 for ind in INDICADORES}
    total_block["VGV"] = Decimal("0")
    fontes = []
    fontes_visitas = set()

    for cid, nome, total, atendeu, agendou, realizada, vendas, vgv, fonte in rows:
        v = visitas_por_corr.get(cid)
        if v is not None:
            agendou_val, realizada_val = int(v[0]), int(v[1])
            fonte_visitas = "visitas"
        else:
            agendou_val, realizada_val = int(agendou), int(realizada)
            fonte_visitas = "vendas_mensais"
        fontes_visitas.add(fonte_visitas)

        bloco = {
            "corretora_id": cid,
            "nome": nome,
            "indicadores": {
                "Total":     int(total),
                "Atendeu":   int(atendeu),
                "Agendou":   agendou_val,
                "Realizada": realizada_val,
                "Vendas":    int(vendas),
                "VGV":       float(vgv),
            },
            "fonte": fonte,
            "fonte_visitas": fonte_visitas,
        }
        by_corr.append(bloco)
        total_block["Total"]     += int(total)
        total_block["Atendeu"]   += int(atendeu)
        total_block["Agendou"]   += agendou_val
        total_block["Realizada"] += realizada_val
        total_block["Vendas"]    += int(vendas)
        total_block["VGV"]       += Decimal(vgv or 0)
        if fonte:
            fontes.append(fonte)

    total_block["VGV"] = float(total_block["VGV"])

    inconsistencias = _inconsistencias("total_geral", total_block)
    for b in by_corr:
        inconsistencias += _inconsistencias(b["nome"], b["indicadores"])

    return {
        "status": "ok",
        "ano_mes": ano_mes,
        "label": target_label,
        "fonte": "postgres.vendas_mensais",
        "fonte_agendados_visitas": sorted(fontes_visitas),
        "fontes_origem": sorted(set(fontes)),
        "inconsistencias": inconsistencias,
        "total_geral": _with_pct(total_block),
        "por_corretora": [
            {
                "corretora_id": b["corretora_id"],
                "nome": b["nome"],
                "fonte": b["fonte"],
                "fonte_visitas": b["fonte_visitas"],
                "indicadores": _with_pct(b["indicadores"]),
            }
            for b in by_corr
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", help="YYYY-MM (default: mês corrente)")
    ap.add_argument("--json", action="store_true", help="só JSON (default já é JSON)")
    args = ap.parse_args()

    year, month = _parse_month(args.month)
    ano_mes = f"{year:04d}-{month:02d}"
    ini, fim = _month_bounds(year, month)

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        _err("DATABASE_URL não setado no ambiente (.env)")
    if dsn.startswith("postgresql+psycopg://"):
        dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(SQL_VENDAS_MENSAIS, (ano_mes,))
        rows = cur.fetchall()

        cur.execute(SQL_VISITAS_MES, (ini, fim))
        visitas_por_corr = {
            int(r[0]): (int(r[1] or 0), int(r[2] or 0)) for r in cur.fetchall()
        }

    if not rows:
        _err("nenhuma corretora ativa encontrada")

    payload = _build_payload(rows, visitas_por_corr, ano_mes)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
