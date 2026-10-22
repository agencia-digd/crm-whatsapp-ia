import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// POST /api/crm/mover  →  POST /painel/crm/mover (B7 — arrastar card no kanban)
// Body: { lead_id, stage_destino, funil? }. funil = "principal" (default) |
// "remarketing" — permite mover o lead pra OUTRO funil e escolher a etapa dele.
// Escopo/validação é o copiloto quem resolve via X-Painel-User (corretora só
// move lead dela; admin qualquer; destino "Follow-up" no principal liga a
// cadência — resposta traz cadencia_ligada). Mover pro remarketing só move a
// etapa (não dispara nutrição — ver copiloto painel.py painel_crm_mover).
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => null);
  if (!body || body.lead_id == null || !body.stage_destino) {
    return NextResponse.json({ error: "body_invalido" }, { status: 400 });
  }
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/crm/mover`, {
      method: "POST",
      headers: {
        "X-Painel-User": viewer.user,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        lead_id: body.lead_id,
        stage_destino: body.stage_destino,
        ...(body.funil ? { funil: body.funil } : {}),
      }),
      cache: "no-store",
    });
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
