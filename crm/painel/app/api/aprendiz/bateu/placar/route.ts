import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Placar do "bateu ou não" (rascunho da IA × resposta real da corretora) via
// copiloto-api /aprendiz/bateu/placar. Heurística local no backend — zero LLM.
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const qs = req.nextUrl.searchParams.toString();
  try {
    const r = await fetch(`${cfg.copilotoUrl}/aprendiz/bateu/placar${qs ? `?${qs}` : ""}`, {
      headers: { "X-Painel-User": viewer.user },
      cache: "no-store",
    });
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
