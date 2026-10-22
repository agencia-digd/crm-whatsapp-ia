import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Ranking de corretoras via copiloto-api /painel/ranking — atendimento%,
// agendamento%, comparecimento%, vendas e VGV por corretora no período.
// Admin-only (dados agregados da equipe inteira): gate aqui + o copiloto
// revalida via X-Painel-User (_require_admin → 403). Somente leitura.
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  if (!viewer.isAdmin) {
    return NextResponse.json({ error: "somente_gestao" }, { status: 403 });
  }
  const sp = req.nextUrl.searchParams;
  const qs = new URLSearchParams({ periodo: sp.get("periodo") || "mes_atual" });
  const inicio = sp.get("inicio");
  const fim = sp.get("fim");
  if (inicio) qs.set("inicio", inicio);
  if (fim) qs.set("fim", fim);
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/ranking?${qs}`, {
      headers: { "X-Painel-User": viewer.user },
      cache: "no-store",
    });
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
