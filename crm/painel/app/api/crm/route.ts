import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Kanban CRM via copiloto-api /painel/crm/board — espinha canônica
// (leads / pipeline_stages / lead_events, tenant Acme). Somente leitura.
// Escopo/privacidade é o copiloto quem resolve via X-Painel-User;
// ?corretora=<id> só tem efeito pra admin.
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const corretora = req.nextUrl.searchParams.get("corretora");
  const qs = corretora ? `?corretora=${encodeURIComponent(corretora)}` : "";
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/crm/board${qs}`, {
      headers: { "X-Painel-User": viewer.user },
      cache: "no-store",
    });
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
