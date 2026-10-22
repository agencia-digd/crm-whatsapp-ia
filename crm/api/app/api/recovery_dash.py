"""Dash leve da Onda Recovery (HSM Acme pela Corretor).

HTML server-side, sem build. Auto-refresh 30s.
Mesmo basic auth do /painel (nginx passa X-Painel-User).
Acessar: app.example.com/painel/recovery
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import text as sql_text

from app.db.session import SessionLocal
from app.api.painel import _identificar_usuario, _require_admin

router = APIRouter(prefix="/painel", tags=["painel"])

# Enum real de recovery_conversations.state (migration 0030 CHECK):
# aguardando · qualificando · reengajado · qualificado · perdido · largado
STATE_LABEL = {
    "aguardando": ("⏳ aguardando", "#9ca3af"),
    "qualificando": ("💬 em atendimento", "#2563eb"),
    "reengajado": ("🔄 reengajado", "#7c3aed"),
    "qualificado": ("✅ qualificado", "#16a34a"),
    "perdido": ("❌ perdido", "#dc2626"),
    "largado": ("🚪 largado", "#a16207"),
}


def _fmt_ts(ts: datetime | None) -> str:
    if not ts:
        return "—"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - ts
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s atrás"
    if s < 3600:
        return f"{s // 60}min atrás"
    if s < 86400:
        return f"{s // 3600}h atrás"
    return f"{s // 86400}d atrás"


def _fetch_metrics() -> dict[str, Any]:
    with SessionLocal() as session:
        agg = session.execute(sql_text("""
            SELECT
                count(*)                                          AS total,
                count(*) FILTER (WHERE state='aguardando')        AS aguardando,
                count(*) FILTER (WHERE state IN ('qualificando','reengajado')) AS em_atendimento,
                count(*) FILTER (WHERE state='qualificado')       AS qualificado,
                count(*) FILTER (WHERE state='perdido')           AS perdido,
                count(*) FILTER (WHERE state='largado')           AS largado,
                count(*) FILTER (WHERE last_lead_msg_at IS NOT NULL) AS responderam,
                count(*) FILTER (WHERE last_corretor_msg_at IS NOT NULL)  AS corretor_falou
            FROM recovery_conversations
            WHERE hsm_sent_at > now() - interval '7 days'
        """)).mappings().one()

        rows = session.execute(sql_text("""
            SELECT
                rc.lead_id, rc.lead_name, rc.lead_phone, rc.state, rc.turns,
                rc.hsm_sent_at, rc.last_lead_msg_at, rc.last_corretor_msg_at,
                rc.end_reason,
                (SELECT text FROM recovery_messages rm
                  WHERE rm.lead_id=rc.lead_id AND rm.role='lead'
                  ORDER BY rm.ts DESC LIMIT 1)                    AS last_lead_text,
                (SELECT text FROM recovery_messages rm
                  WHERE rm.lead_id=rc.lead_id AND rm.role='corretor'
                  ORDER BY rm.ts DESC LIMIT 1)                    AS last_corretor_text
            FROM recovery_conversations rc
            WHERE rc.hsm_sent_at > now() - interval '7 days'
              AND (rc.last_lead_msg_at IS NOT NULL OR rc.last_corretor_msg_at IS NOT NULL)
            ORDER BY
                CASE
                    WHEN rc.state IN ('qualificando','reengajado') THEN 1
                    WHEN rc.state='qualificado' THEN 2
                    WHEN rc.state='aguardando'  THEN 3
                    ELSE 4
                END,
                COALESCE(rc.last_lead_msg_at, rc.hsm_sent_at) DESC
            LIMIT 200
        """)).mappings().all()

    return {"agg": dict(agg), "rows": [dict(r) for r in rows]}


def _render(data: dict[str, Any]) -> str:
    a = data["agg"]
    cards = [
        ("Disparos (7d)",  a["total"],          "#0f172a"),
        ("Responderam",    a["responderam"],    "#2563eb"),
        ("Em atend. IA",   a["em_atendimento"], "#7c3aed"),
        ("Qualificados",   a["qualificado"],    "#16a34a"),
        ("Perdidos",       a["perdido"],        "#dc2626"),
        ("Aguardando",     a["aguardando"],     "#9ca3af"),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="lbl">{lbl}</div>'
        f'<div class="val" style="color:{col}">{val}</div></div>'
        for lbl, val, col in cards
    )

    rows_html = []
    for r in data["rows"]:
        label, color = STATE_LABEL.get(r["state"], (r["state"], "#737373"))
        nome = (r["lead_name"] or "—")[:30]
        last_lead = (r["last_lead_text"] or "").replace("\n", " ")[:140]
        last_corretor = (r["last_corretor_text"] or "").replace("\n", " ")[:140]
        phone = r["lead_phone"][-11:] if r["lead_phone"] else "—"
        rows_html.append(f"""
        <tr>
          <td><strong>{nome}</strong><br><span class="mono small">{phone}</span></td>
          <td><span class="pill" style="background:{color}1a;color:{color}">{label}</span><br>
              <span class="small">turnos: {r["turns"]}</span></td>
          <td class="small">{_fmt_ts(r["hsm_sent_at"])}</td>
          <td class="small">{_fmt_ts(r["last_lead_msg_at"])}<br>
              <span class="msg">{last_lead or "—"}</span></td>
          <td class="small">{_fmt_ts(r["last_corretor_msg_at"])}<br>
              <span class="msg corretor">{last_corretor or "—"}</span></td>
          <td class="small mono">{r["lead_id"]}</td>
        </tr>""")
    rows_str = "".join(rows_html) or '<tr><td colspan="6" style="text-align:center;padding:40px;color:#9ca3af">sem conversas na janela de 7 dias</td></tr>'

    now_sp = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")

    return f"""<!doctype html>
<html lang="pt-BR"><head>
<meta charset="utf-8"><title>Recovery · Acme</title>
<meta http-equiv="refresh" content="10">
<style>
  :root {{ --bg:#faf8f3; --ink:#0f172a; --line:#e7e2d8; --laranja:#e8843a; }}
  * {{ box-sizing:border-box }}
  body {{ margin:0; font-family:-apple-system,system-ui,"Segoe UI",Roboto,sans-serif;
         background:var(--bg); color:var(--ink); }}
  header {{ padding:18px 28px; border-bottom:1px solid var(--line); display:flex;
           justify-content:space-between; align-items:baseline; }}
  h1 {{ margin:0; font-size:20px; font-weight:700; letter-spacing:-0.01em; }}
  h1 span {{ color:var(--laranja); }}
  .meta {{ font-size:12px; color:#737373; font-family:ui-monospace,monospace; }}
  .cards {{ display:grid; grid-template-columns:repeat(6,1fr); gap:12px;
           padding:18px 28px; }}
  .card {{ background:white; border:1px solid var(--line); border-radius:6px;
          padding:14px 16px; }}
  .lbl {{ font-size:11px; text-transform:uppercase; letter-spacing:0.08em;
         color:#737373; font-weight:600; }}
  .val {{ font-size:32px; font-weight:700; margin-top:4px; line-height:1; }}
  table {{ width:calc(100% - 56px); margin:8px 28px 40px; border-collapse:collapse;
          background:white; border:1px solid var(--line); border-radius:6px;
          overflow:hidden; }}
  th, td {{ padding:12px 14px; text-align:left; vertical-align:top;
           border-bottom:1px solid var(--line); }}
  th {{ background:#f5f1e8; font-size:11px; text-transform:uppercase;
        letter-spacing:0.08em; color:#525252; font-weight:600; }}
  tr:last-child td {{ border-bottom:none; }}
  tr:hover {{ background:#fcfaf5; }}
  .pill {{ display:inline-block; padding:2px 9px; border-radius:999px;
          font-size:11px; font-weight:600; }}
  .small {{ font-size:12px; color:#525252; }}
  .mono {{ font-family:ui-monospace,monospace; }}
  .msg {{ display:block; color:#404040; margin-top:3px; font-style:italic; }}
  .msg.corretor {{ color:#2563eb; }}
</style>
</head><body>
  <header>
    <h1>Recovery <span>·</span> Acme <span>·</span> Corretor</h1>
    <span class="meta">atualiza a cada 10s · última carga {now_sp}</span>
  </header>
  <div class="cards">{cards_html}</div>
  <table>
    <thead><tr>
      <th>Lead</th><th>Estado</th><th>HSM</th>
      <th>Última do lead</th><th>Última da Corretor</th><th>Kommo</th>
    </tr></thead>
    <tbody>{rows_str}</tbody>
  </table>
</body></html>"""


@router.get("/recovery", response_class=HTMLResponse)
def recovery_dash(
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> HTMLResponse:
    # Recovery global: todos os leads em recuperação (telefone + texto), sem escopo
    # por corretora. Admin-only — fecha o fail-open de "header presente = acesso".
    _require_admin(_identificar_usuario(x_painel_user))
    data = _fetch_metrics()
    return HTMLResponse(_render(data))


@router.get("/remarketing")
def remarketing_json(
    x_painel_user: str | None = Header(default=None, alias="X-Painel-User"),
) -> dict[str, Any]:
    """JSON pro tab Remarketing do painel React."""
    # Mesmo dado global do /recovery — admin-only.
    _require_admin(_identificar_usuario(x_painel_user))
    data = _fetch_metrics()
    # serializa datetimes
    rows = []
    for r in data["rows"]:
        rows.append({
            "lead_id": r["lead_id"],
            "lead_name": r["lead_name"],
            "lead_phone": r["lead_phone"],
            "state": r["state"],
            "turns": r["turns"],
            "end_reason": r["end_reason"],
            "hsm_sent_at": r["hsm_sent_at"].isoformat() if r["hsm_sent_at"] else None,
            "last_lead_msg_at": r["last_lead_msg_at"].isoformat() if r["last_lead_msg_at"] else None,
            "last_corretor_msg_at": r["last_corretor_msg_at"].isoformat() if r["last_corretor_msg_at"] else None,
            "last_lead_text": r["last_lead_text"],
            "last_corretor_text": r["last_corretor_text"],
        })
    return {"agg": data["agg"], "rows": rows}
