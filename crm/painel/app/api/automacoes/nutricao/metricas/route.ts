import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// GET /api/automacoes/nutricao/metricas?dias=30 → engajamento por MARCO da
// nutrição (30/60/90/120/150 dias) + quantos leads estão parados em cada etapa
// agora. Admin-only aqui e revalidado no copiloto, mesma classe do resto de
// /painel/automacoes.
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  if (!viewer.isAdmin) {
    return NextResponse.json({ error: "somente_gestao" }, { status: 403 });
  }
  const dias = req.nextUrl.searchParams.get("dias") ?? "30";
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/automacoes/nutricao/metricas?dias=${encodeURIComponent(dias)}`,
      { headers: { "X-Painel-User": viewer.user }, cache: "no-store" },
    );
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
