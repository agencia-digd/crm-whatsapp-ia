import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// POST /api/ligacoes/perder  { lead_id, motivo?, corretora_id? }
//   →  POST /painel/ligacoes/perder
// "Venda perdida" na fila de ligação: move o lead pra etapa Venda perdida do
// funil principal e dá dona a quem marcou (sem dona ele sairia da fila e não
// apareceria no board). Sai da fila sozinho — a fila só olha o remarketing.
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => null);
  const leadId = body?.lead_id;
  if (leadId == null || Number.isNaN(Number(leadId))) {
    return NextResponse.json({ error: "lead_id_invalido" }, { status: 400 });
  }
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/ligacoes/perder`, {
      method: "POST",
      headers: {
        "X-Painel-User": viewer.user,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        lead_id: Number(leadId),
        motivo: body?.motivo ?? null,
        corretora_id: body?.corretora_id ?? null,
      }),
      cache: "no-store",
    });
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
