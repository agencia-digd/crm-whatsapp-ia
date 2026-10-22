"""
Registra uma venda no Postgres (vendas).

Uso típico (subprocess via agent.py):
  python3 registrar_venda.py --corretora Pessoa --cliente "Marcos Silva" --valor 350000
  python3 registrar_venda.py --corretora Pessoa11 --cliente "Lurdes" --valor 480000 --month 2026-04

Default --data: hoje. Se --month YYYY-MM, usa primeiro dia do mês.
Vendas são eventos: cada chamada faz INSERT puro (não UPSERT) — duplicatas
naturais (mesmo cliente, mesma data, 2 imóveis) são permitidas.

Saída: JSON em stdout. Erros vão pra stderr com exit !=0.
"""
import argparse
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal

import psycopg


CORR_ALIAS = {
    "corretor": "Pessoa1", "corretora_b": "Pessoa1", "corretora_b": "Pessoa1",
    "corretor": "Pessoa5", "corretora_c": "Pessoa5", "corretora_c": "Pessoa5",
    "pessoa": "Pessoa3", "corretora_a": "Pessoa3", "corretora_a": "Pessoa3", "gio": "Pessoa3",
}


def _err(msg: str, code: int = 1):
    sys.stderr.write(msg + "\n")
    sys.exit(code)


def _data_venda(month_arg: str | None) -> date:
    if not month_arg:
        return date.today()
    try:
        y, m = map(int, month_arg.split("-"))
        return date(y, m, 1)
    except (ValueError, IndexError):
        _err(f"--month inválido: {month_arg} (esperado YYYY-MM)")


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
    ap.add_argument("--cliente", required=True)
    ap.add_argument("--valor", required=True, help="valor em reais (aceita int, decimal)")
    ap.add_argument("--month", help="YYYY-MM (default: data de hoje)")
    ap.add_argument("--observacao", default=None)
    ap.add_argument("--registrado-por-telefone", default=None)
    args = ap.parse_args()

    cliente = args.cliente.strip()
    if not cliente:
        _err("cliente não pode ser vazio")

    try:
        valor = Decimal(str(args.valor).replace(",", "."))
    except Exception:
        _err(f"valor inválido: {args.valor}")
    if valor <= 0:
        _err(f"valor deve ser > 0 (recebido: {valor})")

    data_venda = _data_venda(args.month)

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
                INSERT INTO vendas
                  (corretora_id, cliente_nome, valor, data_venda, observacao,
                   registrado_por_telefone, registrado_em)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                RETURNING id, registrado_em
                """,
                (cid, cliente, valor, data_venda, args.observacao,
                 args.registrado_por_telefone),
            )
            row_id, registrado_em = cur.fetchone()
        conn.commit()

    out = {
        "status": "ok",
        "venda_id": row_id,
        "corretora_id": cid,
        "corretora_nome": nome,
        "valores_gravados": {
            "cliente_nome": cliente,
            "valor": str(valor),
            "data_venda": data_venda.isoformat(),
            "observacao": args.observacao,
        },
        "registrado_em": registrado_em.isoformat(),
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
