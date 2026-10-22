import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Executa uma ação da Assistente JÁ confirmada pela corretora (nada roda sem clique).
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => ({}));
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/assistente/acao`, {
      method: "POST",
      headers: { "X-Painel-User": viewer.user, "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
      signal: AbortSignal.timeout(50000),
    });
    const data = await r.json().catch(() => ({ ok: false, msg: "resposta inválida" }));
    return NextResponse.json(data, { status: r.status });
  } catch {
    return NextResponse.json({ ok: false, msg: "não consegui executar agora 😿" }, { status: 200 });
  }
}
