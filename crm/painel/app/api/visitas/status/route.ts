import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// 📅 Visitas — ação de status: proxy pro copiloto
// POST /painel/visita/{id}/status  body {status: compareceu|no_show|cancelada}.
// Privacidade quem resolve é o copiloto via X-Painel-User (corretora só muda
// visita dela → 403; admin muda qualquer).
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => null);
  const id = Number(body?.id);
  const status = String(body?.status || "");
  if (!Number.isInteger(id) || id <= 0 || !status) {
    return NextResponse.json({ error: "id_e_status_obrigatorios" }, { status: 400 });
  }
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/visita/${id}/status`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Painel-User": viewer.user },
      body: JSON.stringify({ status }),
      cache: "no-store",
    });
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
