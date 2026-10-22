import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// POST /api/ligacoes/renovar → copiloto /painel/ligacoes/renovar
// A aba aberta segura a lista: renova a reserva dos leads que ainda são de
// quem pediu e devolve os `perdidos` (outra corretora pegou, ou alguém já
// registrou a ligação). Caso Corretor, 22/09: a lista parada venceu e a fila da
// Pessoa7 pegou os mesmos leads. Escopo por corretora via X-Painel-User.
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const payload = await req.json().catch(() => ({}));
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/ligacoes/renovar`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Painel-User": viewer.user },
      body: JSON.stringify({ lead_ids: payload?.lead_ids ?? [] }),
      cache: "no-store",
    });
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
