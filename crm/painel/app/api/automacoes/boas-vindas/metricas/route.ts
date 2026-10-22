import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// GET /api/automacoes/boas-vindas/metricas?dias=30 → quantas boas-vindas
// saíram, quantas foram respondidas, como a roleta distribuiu e o placar do
// teste A/B da primeira mensagem.
// Admin-only aqui e revalidado no copiloto — é número agregado da operação
// inteira, mesma classe do resto de /painel/automacoes.
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  if (!viewer.isAdmin) {
    return NextResponse.json({ error: "somente_gestao" }, { status: 403 });
  }
  const dias = req.nextUrl.searchParams.get("dias") ?? "30";
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/automacoes/boas-vindas/metricas?dias=${encodeURIComponent(dias)}`,
      { headers: { "X-Painel-User": viewer.user }, cache: "no-store" },
    );
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
