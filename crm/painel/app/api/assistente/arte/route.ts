import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Assistente — compõe arte branded (foto anexada + brand Acme) via copiloto.
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => ({}));
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/assistente/arte`, {
      method: "POST",
      headers: { "X-Painel-User": viewer.user, "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
      signal: AbortSignal.timeout(60000),
    });
    const data = await r.json().catch(() => ({ erro: "resposta inválida" }));
    return NextResponse.json(data, { status: r.status });
  } catch {
    return NextResponse.json({ erro: "não consegui montar a arte agora" }, { status: 200 });
  }
}
