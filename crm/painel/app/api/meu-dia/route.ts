import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// GET /api/meu-dia → copiloto /painel/meu-dia
//
// Proxy READ-ONLY da tela "👋 Meu dia": os 5 blocos do dia da corretora, já
// ordenados por CUSTO DE DEMORA no backend (o front NÃO reordena).
// Escopo/privacidade é do copiloto (X-Painel-User): corretora vê só o dela.
//
// `?corretora=` (filtro de pessoa das pills) é REPASSADO cru — quem decide se
// vale é o copiloto, que só o honra pra admin e o ignora pra corretora. O proxy
// não é lugar de decidir permissão: ele já erra fail-closed no resolveViewer.
//
// Copiloto fora do ar devolve 502 com `error` — a tela mostra "não consegui
// carregar", nunca "tudo em dia". "Sem dado" ≠ zero.
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const limite = req.nextUrl.searchParams.get("limite") ?? "25";
  const corretora = req.nextUrl.searchParams.get("corretora");
  const qs = corretora ? `&corretora=${encodeURIComponent(corretora)}` : "";
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/meu-dia?limite=${encodeURIComponent(limite)}${qs}`,
      { headers: { "X-Painel-User": viewer.user }, cache: "no-store" },
    );
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
