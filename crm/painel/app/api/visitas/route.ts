import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// 📅 Visitas — proxy pro copiloto /painel/agenda (mesmo endpoint que o V1
// consumia em /api/agenda). Devolve total, por_status, taxa_comparecimento e
// items (visita a visita, com data/hora em SP). Escrita de status vive em
// ./status/route.ts → copiloto POST /painel/visita/{id}/status.
// Escopo/privacidade quem resolve é o copiloto via X-Painel-User:
//   admin (dona/assistente/pessoa) → todas; corretora → só as dela.
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const periodo = req.nextUrl.searchParams.get("periodo") || "semana";
  const qs = `?periodo=${encodeURIComponent(periodo)}`;
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/agenda${qs}`, {
      headers: { "X-Painel-User": viewer.user },
      cache: "no-store",
    });
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
