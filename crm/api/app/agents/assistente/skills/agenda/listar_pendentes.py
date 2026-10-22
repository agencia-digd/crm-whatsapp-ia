"""
Lista visitas 🟠 (cor laranja) da semana passada que ainda não foram confirmadas.

Uso:
  python3 listar_pendentes.py
  python3 listar_pendentes.py --json     # output JSON
  python3 listar_pendentes.py --weeks 2  # 2 semanas atrás
"""
import argparse
import json
import sys
from _helper import (calendar_request, cal_path, week_range_iso,
                     COLOR_PLANNED, extract_corretora_from_summary, CORRETORAS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--weeks", type=int, default=1)
    args = ap.parse_args()

    time_min, time_max = week_range_iso(args.weeks)
    resp = calendar_request("GET", cal_path(), params={
        "timeMin": time_min,
        "timeMax": time_max,
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": "250",
    })
    items = resp.get("items", [])
    pendentes = [e for e in items if str(e.get("colorId", "")) == COLOR_PLANNED]

    out = []
    for e in pendentes:
        prefixo = extract_corretora_from_summary(e.get("summary", ""))
        info = CORRETORAS.get(prefixo) if prefixo else None
        out.append({
            "event_id": e["id"],
            "summary": e.get("summary"),
            "start": e.get("start", {}).get("dateTime") or e.get("start", {}).get("date"),
            "corretora_prefixo": prefixo,
            "corretora_nome": info["nome"] if info else None,
            "whatsapp_alvo": info["whatsapp"][0] if info else None,
        })

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    print(f"Pendentes na semana {time_min[:10]} → {time_max[:10]}: {len(out)}")
    for v in out:
        print(f"  🟠 {v['summary']:<35} {v['start'][:16]}  → {v['corretora_nome'] or '?'}")


if __name__ == "__main__":
    main()
