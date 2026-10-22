import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// O que o Aprendiz já aprendeu e o que mudou, via copiloto-api /aprendiz/aprendizado.
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const qs = req.nextUrl.searchParams.toString();
  try {
    const r = await fetch(`${cfg.copilotoUrl}/aprendiz/aprendizado${qs ? `?${qs}` : ""}`, {
      headers: { "X-Painel-User": viewer.user },
      cache: "no-store",
    });
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
