import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Placar de autonomia do Aprendiz (modo sombra) via copiloto-api
// /aprendiz/placar. Somente leitura; escopo/privacidade é o copiloto quem
// resolve via X-Painel-User (?corretora_id só tem efeito pra admin).
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const qs = req.nextUrl.searchParams.toString();
  try {
    const r = await fetch(`${cfg.copilotoUrl}/aprendiz/placar${qs ? `?${qs}` : ""}`, {
      headers: { "X-Painel-User": viewer.user },
      cache: "no-store",
    });
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
