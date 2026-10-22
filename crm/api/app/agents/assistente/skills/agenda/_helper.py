"""Helpers compartilhados: Gateway Calendar API + envio WhatsApp via openclaw CLI."""
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------- Calendar (via Gateway) ----------

CALENDAR_ID = "contato@example.com"
GATEWAY_BASE = "https://gateway.gateway.ai/google-calendar/calendar/v3"

# colorId Google Calendar
COLOR_PLANNED  = "6"    # 🟠 laranja  — visita planejada
COLOR_SHOWED   = "10"   # 🟢 verde    — compareceu
COLOR_NOSHOW   = "11"   # 🔴 vermelho — no-show
COLOR_CANCELED = "8"    # ⚫ grafite  — cancelada

CORRETORAS = {
    # prefixo do título → (nome humano, números whatsapp possíveis)
    "Pessoa11":  {"nome": "Pessoa1", "whatsapp": ["+5500000000000"]},
    "Pessoa10":  {"nome": "Pessoa6",    "whatsapp": ["+5500000000000"]},
    "Pessoa": {"nome": "Pessoa3 (Pessoa Pessoa9)",
              "whatsapp": ["+5500000000000", "+5500000000000", "+5500000000000"]},
}


def _gateway_token():
    token = os.environ.get("GATEWAY_API_KEY")
    if not token:
        raise SystemExit("GATEWAY_API_KEY não setado no ambiente (.env)")
    return token


def _gateway_headers():
    return {
        "Authorization": f"Bearer {_gateway_token()}",
        "Content-Type": "application/json",
    }


def calendar_request(method, path, body=None, params=None):
    url = GATEWAY_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers=_gateway_headers())
    with urllib.request.urlopen(req, timeout=30) as r:
        if r.status == 204:
            return {}
        return json.loads(r.read())


def cal_path(suffix=""):
    return f"/calendars/{urllib.parse.quote(CALENDAR_ID)}/events{suffix}"


# ---------- WhatsApp (via openclaw CLI) ----------

def whatsapp_send(target, message, dry_run=False):
    """Envia mensagem WhatsApp via CLI nativa do OpenClaw."""
    cmd = [
        "openclaw", "message", "send",
        "--channel", "whatsapp",
        "--target", target,
        "--message", message,
        "--json",
    ]
    if dry_run:
        cmd.append("--dry-run")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip() or result.stdout.strip()}
    try:
        out = json.loads(result.stdout) if result.stdout.strip() else {"ok": True}
    except json.JSONDecodeError:
        out = {"ok": True, "raw": result.stdout.strip()}
    return out


# ---------- Helpers gerais ----------

def parse_corretora_arg(s):
    """Aceita 'Pessoa11', 'Pessoa10', 'Pessoa', 'Pessoa1', 'Pessoa6', 'Pessoa3'."""
    s = s.strip()
    sl = s.lower()
    aliases = {
        "corretor": "Pessoa11", "corretora_b": "Pessoa11", "corretora_b": "Pessoa11",
        "corretor": "Pessoa10", "corretora_c": "Pessoa10", "corretora_c": "Pessoa10",
        "pessoa": "Pessoa", "corretora_a": "Pessoa", "corretora_a": "Pessoa", "gio": "Pessoa",
    }
    if sl in aliases:
        return aliases[sl]
    raise SystemExit(f"corretora não reconhecida: {s}. Use Pessoa11/Pessoa10/Pessoa")


def extract_corretora_from_summary(summary):
    """'[Pessoa11] Poliana' → 'Pessoa11'. None se não bater."""
    if not summary:
        return None
    m = re.match(r"^\[(\w+)\]", summary)
    return m.group(1) if m else None


def to_brt_iso(dt_str):
    """Converte 'YYYY-MM-DDTHH:MM' (sem tz) → 'YYYY-MM-DDTHH:MM:SS-03:00' (BRT)."""
    if "T" not in dt_str:
        raise SystemExit(f"datetime inválido: {dt_str} (use 2026-04-29T14:00)")
    if "-03:00" in dt_str or "+" in dt_str.split("T")[1]:
        return dt_str
    if dt_str.count(":") == 1:
        dt_str += ":00"
    return dt_str + "-03:00"


def week_range_iso(weeks_back=1):
    """Retorna (timeMin, timeMax) ISO da semana passada (segunda 00h → domingo 23:59)."""
    today = datetime.now(timezone(timedelta(hours=-3)))
    # Domingo passado às 23:59
    days_back_to_sunday = today.weekday() + 1  # Mon=0, ..., Sun=6
    last_sunday_end = (today - timedelta(days=days_back_to_sunday)).replace(
        hour=23, minute=59, second=59, microsecond=0
    )
    last_monday_start = (last_sunday_end - timedelta(days=6)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return last_monday_start.isoformat(), last_sunday_end.isoformat()
