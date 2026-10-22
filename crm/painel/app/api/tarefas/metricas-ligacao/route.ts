import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// GET /api/tarefas/metricas-ligacao?dias=30 → copiloto /painel/tarefas/metricas-ligacao
// Ligações feitas · atenderam · quiseram continuar, por corretora e no total —
// os três números que a Dona media na mão. Admin-only aqui e revalidado no
// copiloto (é performance de toda a equipe).
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  if (!viewer.isAdmin) {
    return NextResponse.json({ error: "somente_gestao" }, { status: 403 });
  }
  const dias = req.nextUrl.searchParams.get("dias") ?? "30";
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/tarefas/metricas-ligacao?dias=${encodeURIComponent(dias)}`,
      { headers: { "X-Painel-User": viewer.user }, cache: "no-store" },
    );
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
