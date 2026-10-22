import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// POST /api/crm/transferir  →  POST /painel/crm/transferir
// Body: { lead_id, corretora_destino, motivo? }. Transfere o lead de corretora:
// muda owner_id na espinha + escopo da thread/coaching, grava lead_event
// tipo='transferencia'. Escopo/validação é o copiloto quem resolve via
// X-Painel-User (admin transfere qualquer lead; corretora só lead DELA — 403
// lead_de_outra_corretora). ZERO WhatsApp — aviso é in-app (selo "transferido"
// no card do chat por 7 dias).
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => null);
  if (!body || body.lead_id == null || body.corretora_destino == null) {
    return NextResponse.json({ error: "body_invalido" }, { status: 400 });
  }
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/crm/transferir`, {
      method: "POST",
      headers: {
        "X-Painel-User": viewer.user,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        lead_id: body.lead_id,
        corretora_destino: body.corretora_destino,
        ...(body.motivo ? { motivo: body.motivo } : {}),
      }),
      cache: "no-store",
    });
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
