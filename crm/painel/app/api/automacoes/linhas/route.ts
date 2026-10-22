import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// GET /api/automacoes/linhas[?forcar=true]
//
// Saúde de TODAS as linhas do cliente numa leitura só: dona, número, papel,
// qualidade na Meta e se a linha está recusando por cobrança. O copiloto guarda
// 60 s de cache (a saúde da Meta tem os 5 min dela); `forcar` relê o cadastro.
// Somente leitura.
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  if (!viewer.isAdmin) {
    return NextResponse.json({ error: "somente_gestao" }, { status: 403 });
  }
  const forcar = req.nextUrl.searchParams.get("forcar") === "true" ? "?forcar=true" : "";
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/automacoes/linhas${forcar}`, {
      headers: { "X-Painel-User": viewer.user },
      cache: "no-store",
    });
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
