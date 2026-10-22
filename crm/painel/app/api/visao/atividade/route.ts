import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Visão · mensagens e ligações por corretora → copiloto /painel/visao/atividade.
// Mesmo período do seletor da tela. Somente leitura. Escopo pelo X-Painel-User
// (admin vê o time; corretora, só a linha dela).
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const periodo = req.nextUrl.searchParams.get("periodo") || "mes_atual";
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/visao/atividade?periodo=${encodeURIComponent(periodo)}`,
      { headers: { "X-Painel-User": viewer.user }, cache: "no-store" },
    );
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
