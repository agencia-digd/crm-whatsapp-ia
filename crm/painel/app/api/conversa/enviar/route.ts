import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Envio de texto livre PRO LEAD (dentro da janela de 24h) — roteia pro copiloto
// (POST /painel/conversa/enviar), que manda pela linha da corretora dona (Cloud
// API pra Pessoa12, Evolution pras demais). Fora da janela o front bloqueia esse
// caminho e oferece reengajamento (template UTILITY). Privacidade + DRY_RUN são
// do copiloto (X-Painel-User / settings.dry_run).

// POST /api/conversa/enviar  { lead_id, texto }  →  POST /painel/conversa/enviar
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => null);
  if (!body || !body.lead_id || !body.texto) {
    return NextResponse.json({ error: "body_invalido" }, { status: 400 });
  }
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/conversa/enviar`, {
      method: "POST",
      headers: {
        "X-Painel-User": viewer.user,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ lead_id: body.lead_id, texto: body.texto }),
      cache: "no-store",
    });
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
