import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Painel VISÃO (dashboard gerencial) via copiloto-api /painel/visao-geral.
// Devolve funil da equipe, KPIs (Meta Ads, VGV), funil por corretora,
// tendência 14d, vazamento, alertas etc. Somente leitura.
// Escopo/privacidade quem resolve é o copiloto via X-Painel-User:
//   admin (dona/assistente/pessoa) → tudo; corretora → só o dela, sem Meta Ads/
//   investimento/comparativo. Aqui só repassamos o header e o ?periodo=.
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const periodo = req.nextUrl.searchParams.get("periodo") || "mes_atual";
  const qs = `?periodo=${encodeURIComponent(periodo)}`;
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/visao-geral${qs}`, {
      headers: { "X-Painel-User": viewer.user },
      cache: "no-store",
    });
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
