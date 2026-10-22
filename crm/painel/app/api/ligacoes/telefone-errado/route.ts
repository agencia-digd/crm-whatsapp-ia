import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// POST /api/ligacoes/telefone-errado  { lead_id, observacao?, corretora_id? }
//   →  POST /painel/ligacoes/telefone-errado
// "Número errado" na fila de ligação: marca o telefone como inválido e tira o
// lead da fila PRA SEMPRE. Não registra resultado de ligação — número que não
// completa a chamada não é "não atendeu", e contar como tal afundava a taxa de
// atendimento da corretora com um problema de cadastro. Antes deste botão o
// único caminho era "não atendeu", e o lead voltava a cada 24h.
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => null);
  const leadId = body?.lead_id;
  if (leadId == null || Number.isNaN(Number(leadId))) {
    return NextResponse.json({ error: "lead_id_invalido" }, { status: 400 });
  }
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/ligacoes/telefone-errado`, {
      method: "POST",
      headers: {
        "X-Painel-User": viewer.user,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        lead_id: Number(leadId),
        observacao: body?.observacao ?? null,
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
