import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// GET /api/tarefas/concluidas?page=1  →  GET /painel/tarefas/concluidas
// Histórico paginado das tarefas concluídas (aba "Concluídas" da tela ☑️
// Tarefas): mais recente primeiro, com quem concluiu (`concluido_por`,
// migration 0062 — linhas antigas vêm null) e quando (`concluido_em`).
// Escopo quem resolve é o copiloto via X-Painel-User (corretora só as dela).
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const page = req.nextUrl.searchParams.get("page") ?? "1";
  const porPagina = req.nextUrl.searchParams.get("por_pagina") ?? "20";
  // ?corretora= só vale pra admin (o copiloto ignora se for corretora)
  const corr = req.nextUrl.searchParams.get("corretora");
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/tarefas/concluidas?page=${encodeURIComponent(page)}&por_pagina=${encodeURIComponent(porPagina)}${corr ? `&corretora=${encodeURIComponent(corr)}` : ""}`,
      { headers: { "X-Painel-User": viewer.user }, cache: "no-store" },
    );
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
