"""
Registra ligações semanais de uma corretora no Postgres (ligacoes_semanais).

Uso típico (subprocess via agent.py):
  python3 registrar_ligacoes.py --corretora Pessoa11 --ligacoes 87 --atendeu 14 --prosseguiu 8
  python3 registrar_ligacoes.py --corretora Pessoa --ligacoes 23 --atendeu 5 --prosseguiu 2 --semana 2026-W20

Default --semana: ISO week da data atual (formato YYYY-Www).

Saída: JSON em stdout. Erros vão pra stderr com exit !=0.
"""
import argparse
import json
import os
import sys
from datetime import datetime

import psycopg


CORR_ALIAS = {
    "corretor": "Pessoa1", "corretora_b": "Pessoa1", "corretora_b": "Pessoa1",
    "corretor": "Pessoa5", "corretora_c": "Pessoa5", "corretora_c": "Pessoa5",
    "pessoa": "Pessoa3", "corretora_a": "Pessoa3", "corretora_a": "Pessoa3", "gio": "Pessoa3",
}


def _err(msg: str, code: int = 1):
    sys.stderr.write(msg + "\n")
    sys.exit(code)


def _iso_week(arg: str | None) -> str:
    if arg:
        s = arg.strip().upper()
        try:
            y, w = s.split("-W")
            datetime.strptime(f"{int(y)}-W{int(w):02d}-1", "%G-W%V-%u")
            return f"{int(y)}-W{int(w):02d}"
        except (ValueError, IndexError):
            _err(f"--semana inválida: {arg} (esperado YYYY-Www, ex: 2026-W20)")
    y, w, _ = datetime.now().isocalendar()
    return f"{y}-W{w:02d}"


def _resolve_corretora(conn, corretora_arg: str) -> tuple[int, str]:
    key = corretora_arg.strip().lower()
    nome_db = CORR_ALIAS.get(key)
    if not nome_db:
        _err(f"corretora não reconhecida: {corretora_arg}. Use Pessoa11/Pessoa10/Pessoa")
    with conn.cursor() as cur:
        cur.execute("SELECT id, nome, ativo FROM corretoras WHERE nome = %s", (nome_db,))
        row = cur.fetchone()
    if not row:
        _err(f"corretora '{nome_db}' não encontrada na tabela corretoras")
    cid, nome, ativo = row
    if not ativo:
        _err(f"corretora '{nome}' está inativa")
    return cid, nome


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corretora", required=True)
    ap.add_argument("--ligacoes", type=int, required=True)
    ap.add_argument("--atendeu", type=int, required=True)
    ap.add_argument("--prosseguiu", type=int, required=True)
    ap.add_argument("--semana", help="ISO week YYYY-Www (default: semana corrente)")
    ap.add_argument("--registrado-por-telefone", default=None)
    args = ap.parse_args()

    if args.ligacoes < 0 or args.atendeu < 0 or args.prosseguiu < 0:
        _err("valores devem ser >= 0")
    if args.atendeu > args.ligacoes:
        _err(f"atendeu ({args.atendeu}) > ligacoes ({args.ligacoes})")
    if args.prosseguiu > args.atendeu:
        _err(f"prosseguiu ({args.prosseguiu}) > atendeu ({args.atendeu})")

    semana = _iso_week(args.semana)

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        _err("DATABASE_URL não setado no ambiente (.env)")
    if dsn.startswith("postgresql+psycopg://"):
        dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)

    with psycopg.connect(dsn) as conn:
        cid, nome = _resolve_corretora(conn, args.corretora)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ligacoes_semanais
                  (corretora_id, ano_semana_iso, ligacoes, atendeu, prosseguiu,
                   registrado_por_telefone, registrado_em, atualizado_em)
                VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (corretora_id, ano_semana_iso) DO UPDATE
                  SET ligacoes = EXCLUDED.ligacoes,
                      atendeu = EXCLUDED.atendeu,
                      prosseguiu = EXCLUDED.prosseguiu,
                      registrado_por_telefone = COALESCE(EXCLUDED.registrado_por_telefone,
                                                        ligacoes_semanais.registrado_por_telefone),
                      atualizado_em = NOW()
                RETURNING id, registrado_em, atualizado_em
                """,
                (cid, semana, args.ligacoes, args.atendeu, args.prosseguiu,
                 args.registrado_por_telefone),
            )
            row_id, registrado_em, atualizado_em = cur.fetchone()
        conn.commit()

    out = {
        "status": "ok",
        "linha_id": row_id,
        "corretora_id": cid,
        "corretora_nome": nome,
        "ano_semana_iso": semana,
        "valores_gravados": {
            "ligacoes": args.ligacoes,
            "atendeu": args.atendeu,
            "prosseguiu": args.prosseguiu,
        },
        "registrado_em": registrado_em.isoformat(),
        "atualizado_em": atualizado_em.isoformat(),
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
