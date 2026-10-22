"""
Adiciona uma nota (comment do Sheets) numa célula da aba "Funil Semanal".
A nota fica como anotação visível ao passar o mouse — não sobrescreve o conteúdo.

Uso:
  python3 add_note.py --cell B11 --text "Maio: cobrar update na Kommo"
  python3 add_note.py --row 11 --col 2 --text "..."
"""
import argparse
from _helper import open_sheet


def col_to_letter(idx_1based):
    out = ""
    n = idx_1based
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(ord("A") + r) + out
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cell", help="endereço A1 (ex: B11)")
    ap.add_argument("--row", type=int, help="linha 1-indexed")
    ap.add_argument("--col", type=int, help="coluna 1-indexed")
    ap.add_argument("--text", required=True)
    ap.add_argument("--tab", default="Funil Semanal")
    args = ap.parse_args()

    if not args.cell:
        if not (args.row and args.col):
            raise SystemExit("informe --cell A1 ou --row N --col N")
        args.cell = f"{col_to_letter(args.col)}{args.row}"

    sh = open_sheet()
    ws = sh.worksheet(args.tab)
    sid = ws.id

    # Parse cell pra get row/col
    import re
    m = re.match(r"([A-Z]+)(\d+)", args.cell.upper())
    if not m:
        raise SystemExit(f"cell inválido: {args.cell}")
    col_letters, row_str = m.groups()
    col = 0
    for ch in col_letters:
        col = col * 26 + (ord(ch) - ord("A") + 1)
    row = int(row_str)

    sh.batch_update({"requests": [{
        "updateCells": {
            "range": {
                "sheetId": sid,
                "startRowIndex": row - 1,
                "endRowIndex": row,
                "startColumnIndex": col - 1,
                "endColumnIndex": col,
            },
            "rows": [{"values": [{"note": args.text}]}],
            "fields": "note",
        }
    }]})
    print(f"✓ nota adicionada em {args.cell}: {args.text!r}")


if __name__ == "__main__":
    main()
