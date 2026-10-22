import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Histórico anual via copiloto-api /painel/historico — série mês-a-mês do ano
// (Meta Ads spend/CPL + funil da equipe + breakdown por corretora + totais).
// Admin-only (traz spend/ROI de todas as corretoras): gate aqui + o copiloto
// revalida via X-Painel-User. Somente leitura.
// O copiloto mantém cache persistente (historico_cache): mês fechado é servido
// do cache; só o mês corrente é recalculado (TTL 15 min). Chamada fria (cache
// vazio ou ?force=1) ainda pode levar ~1 min. `force=1` invalida o cache do
// ano e recalcula tudo (útil quando venda/lead entra retroativo em mês fechado).
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  if (!viewer.isAdmin) {
    return NextResponse.json({ error: "somente_gestao" }, { status: 403 });
  }
  const ano = req.nextUrl.searchParams.get("ano");
  const force = req.nextUrl.searchParams.get("force");
  const params = new URLSearchParams();
  if (ano) params.set("ano", ano);
  if (force) params.set("force", force);
  const qs = params.size ? `?${params.toString()}` : "";
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/historico${qs}`, {
      headers: { "X-Painel-User": viewer.user },
      cache: "no-store",
    });
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
