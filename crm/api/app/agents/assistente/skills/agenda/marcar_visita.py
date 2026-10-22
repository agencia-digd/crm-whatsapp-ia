"""
Marca uma visita como compareceu / no_show / cancelada — atualiza Gateway + Postgres.

Comportamento UPSERT: se o evento existe no Calendar mas não tem linha em
`visitas` (eventos antigos pré-migração), faz GET no Gateway, infere
corretora pelo prefixo do título "[Prefixo] Cliente" e cria entrada mínima
antes de atualizar o status.

Ordem:
  1. Lookup visita por evento_calendar_id no Postgres
  2. Se ausente → GET Gateway + INSERT mínimo (UPSERT)
  3. Se status já é o target → retorna info, sai (idempotente)
  4. PATCH Gateway (colorId)
  5. UPDATE Postgres status + atualizado_em
  6. Se Gateway OK mas UPDATE Postgres falha → warning loud

Uso:
  python3 marcar_visita.py --event-id ABC123 --status compareceu
  python3 marcar_visita.py --event-id ABC123 --status no_show
  python3 marcar_visita.py --event-id ABC123 --status cancelada
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

import psycopg

from _helper import (calendar_request, cal_path,
                     COLOR_SHOWED, COLOR_NOSHOW, COLOR_CANCELED)


STATUS_COLOR = {
    "compareceu": COLOR_SHOWED,
    "no_show":    COLOR_NOSHOW,
    "cancelada":  COLOR_CANCELED,
}

PREFIXO_DB = {
    "Pessoa11":  "Pessoa1",
    "Pessoa10":  "Pessoa5",
    "Pessoa": "Pessoa3",
}


def _err(msg: str, code: int = 1):
    sys.stderr.write(msg + "\n")
    sys.exit(code)


def _dsn() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        _err("DATABASE_URL não setado no ambiente (.env)")
    if dsn.startswith("postgresql+psycopg://"):
        dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
    return dsn


def _backfill_from_gateway(conn, event_id: str) -> tuple[int, str]:
    """Pra evento sem linha em visitas: lê detalhes no Gateway e INSERT mínimo."""
    try:
        ev = calendar_request("GET", cal_path(f"/{event_id}"))
    except Exception as e:
        _err(f"evento {event_id} não encontrado no Calendar nem no Postgres: {e}")

    summary = (ev.get("summary") or "").strip()
    m = re.match(r"^\[(\w+)\]\s*(.+)$", summary)
    if not m:
        _err(f"summary do evento não tem prefixo '[Corretora]': {summary!r}")
    prefixo, lead_nome = m.group(1), m.group(2).strip()
    nome_db = PREFIXO_DB.get(prefixo)
    if not nome_db:
        _err(f"prefixo desconhecido no evento ({prefixo}); summary={summary!r}")

    start = (ev.get("start") or {}).get("dateTime")
    if not start:
        _err(f"evento sem start.dateTime: {event_id}")
    data_hora = datetime.fromisoformat(start)

    with conn.cursor() as cur:
        cur.execute("SELECT id FROM corretoras WHERE nome = %s AND ativo = TRUE",
                    (nome_db,))
        row = cur.fetchone()
        if not row:
            _err(f"corretora '{nome_db}' inferida do evento não está ativa")
        cid = row[0]
        cur.execute(
            """
            INSERT INTO visitas
              (corretora_id, evento_calendar_id, lead_nome, data_hora,
               status, criado_em, atualizado_em)
            VALUES (%s, %s, %s, %s, 'agendada', NOW(), NOW())
            RETURNING id
            """,
            (cid, event_id, lead_nome, data_hora),
        )
        visita_id = cur.fetchone()[0]
    conn.commit()
    return visita_id, "agendada"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--event-id", required=True)
    ap.add_argument("--status", required=True, choices=list(STATUS_COLOR.keys()))
    ap.add_argument("--status-atualizado-por-telefone", default=None)
    args = ap.parse_args()

    if not args.event_id.strip():
        _err("evento_id vazio")

    target_status = args.status
    target_color = STATUS_COLOR[target_status]
    backfilled = False

    with psycopg.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status, lead_nome FROM visitas WHERE evento_calendar_id = %s",
                (args.event_id,),
            )
            row = cur.fetchone()

        if row:
            visita_id, current_status, lead_nome = row
        else:
            visita_id, current_status = _backfill_from_gateway(conn, args.event_id)
            backfilled = True
            with conn.cursor() as cur:
                cur.execute("SELECT lead_nome FROM visitas WHERE id = %s", (visita_id,))
                lead_nome = cur.fetchone()[0]

        # 3) idempotência
        if current_status == target_status:
            out = {
                "status": "ok",
                "info": "already_set",
                "visita_id": visita_id,
                "evento_calendar_id": args.event_id,
                "lead_nome": lead_nome,
                "status_atual": current_status,
                "backfilled": backfilled,
            }
            print(json.dumps(out, ensure_ascii=False))
            return

        # 4) PATCH Gateway
        try:
            resp = calendar_request("PATCH", cal_path(f"/{args.event_id}"),
                                    body={"colorId": target_color})
        except Exception as e:
            _err(f"falha PATCH Gateway: {e}")

        # 5) UPDATE Postgres
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE visitas
                       SET status = %s,
                           atualizado_em = NOW(),
                           status_atualizado_por_tel = COALESCE(%s, status_atualizado_por_tel)
                     WHERE id = %s
                    RETURNING atualizado_em
                    """,
                    (target_status, args.status_atualizado_por_telefone, visita_id),
                )
                atualizado_em = cur.fetchone()[0]
            conn.commit()
        except Exception as e:
            sys.stderr.write(
                f"WARN: Gateway PATCH OK mas UPDATE Postgres falhou. "
                f"evento_calendar_id={args.event_id} status_alvo={target_status}. "
                f"Avise admin. erro={e}\n"
            )
            out = {
                "status": "warning",
                "warning": "gateway_ok_postgres_failed",
                "evento_calendar_id": args.event_id,
                "summary": resp.get("summary"),
                "error": str(e),
            }
            print(json.dumps(out, ensure_ascii=False))
            sys.exit(2)

    out = {
        "status": "ok",
        "visita_id": visita_id,
        "evento_calendar_id": args.event_id,
        "lead_nome": lead_nome,
        "status_anterior": current_status,
        "status_novo": target_status,
        "backfilled": backfilled,
        "summary": resp.get("summary"),
        "atualizado_em": atualizado_em.isoformat(),
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
