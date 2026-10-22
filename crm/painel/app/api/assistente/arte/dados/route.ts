import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Só extrai os dados do empreendimento pelo texto pra corretora confirmar antes de gerar a arte.
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => ({}));
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/assistente/arte/dados`, {
      method: "POST",
      headers: { "X-Painel-User": viewer.user, "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
      signal: AbortSignal.timeout(40000),
    });
    const data = await r.json().catch(() => ({ dados: {} }));
    return NextResponse.json(data, { status: r.status });
  } catch {
    return NextResponse.json({ dados: {} }, { status: 200 });
  }
}
