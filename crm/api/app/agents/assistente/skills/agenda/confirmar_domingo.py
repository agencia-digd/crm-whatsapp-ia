"""
Cron domingo 20h SP: pra cada visita 🟠 da semana passada,
manda WhatsApp pra corretora pedindo confirmação se aconteceu.

Uso:
  python3 confirmar_domingo.py             # roda
  python3 confirmar_domingo.py --dry-run   # só imprime o que mandaria
"""
import argparse
import sys
from _helper import (calendar_request, cal_path, week_range_iso,
                     COLOR_PLANNED, extract_corretora_from_summary,
                     CORRETORAS, whatsapp_send)


TEMPLATE = """\
Oi! 🌹 Aqui é a Assistente.

Tô fechando a semana — pode me confirmar se essas visitas aconteceram?

{lista}

Pra cada uma, me responde com:
✅ apareceu  ou  ❌ deu bolo  (e o nome do cliente).

Obrigada! 💛"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    time_min, time_max = week_range_iso(1)
    resp = calendar_request("GET", cal_path(), params={
        "timeMin": time_min,
        "timeMax": time_max,
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": "250",
    })
    items = resp.get("items", [])
    pendentes = [e for e in items if str(e.get("colorId", "")) == COLOR_PLANNED]

    # agrupa por corretora
    by_corr = {}
    for e in pendentes:
        prefixo = extract_corretora_from_summary(e.get("summary", ""))
        if not prefixo or prefixo not in CORRETORAS:
            continue
        by_corr.setdefault(prefixo, []).append(e)

    if not by_corr:
        print(f"Sem visitas pendentes na semana {time_min[:10]} → {time_max[:10]}.")
        return

    for prefixo, eventos in by_corr.items():
        info = CORRETORAS[prefixo]
        target = info["whatsapp"][0]  # número principal
        lista_str = ""
        for e in eventos:
            start = e.get("start", {}).get("dateTime", "")[:16].replace("T", " ")
            lista_str += f"  • {e.get('summary', '?')} — {start}\n"
        msg = TEMPLATE.format(lista=lista_str.rstrip())

        print(f"→ {info['nome']} ({target}): {len(eventos)} visita(s)")
        if args.dry_run:
            print(f"   {msg}\n")
            continue
        result = whatsapp_send(target, msg)
        if result.get("ok") is False:
            print(f"   ❌ falha: {result.get('error')}")
        else:
            print(f"   ✓ enviado")


if __name__ == "__main__":
    main()
