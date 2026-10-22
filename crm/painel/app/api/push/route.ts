import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Proxy das rotas de Web Push do copiloto.
//
// GET  /api/push            → chave pública VAPID + nº de aparelhos ligados
// POST /api/push?acao=...   → inscrever | desinscrever | testar
//
// A corretora NUNCA viaja no corpo: o copiloto resolve pelo X-Painel-User.
// Se viesse do body, qualquer uma inscreveria um aparelho no nome de outra e
// passaria a receber os leads dela.

export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/push/chave`, {
      headers: { "X-Painel-User": viewer.user },
      cache: "no-store",
    });
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_fora" }, { status: 502 });
  }
}

const ACOES: Record<string, string> = {
  inscrever: "/painel/push/inscrever",
  desinscrever: "/painel/push/desinscrever",
  testar: "/painel/push/testar",
};

export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const acao = req.nextUrl.searchParams.get("acao") ?? "";
  const rota = ACOES[acao];
  if (!rota) {
    return NextResponse.json({ error: "acao_invalida" }, { status: 400 });
  }

  const payload = acao === "testar" ? {} : await req.json().catch(() => ({}));

  try {
    const r = await fetch(`${cfg.copilotoUrl}${rota}`, {
      method: "POST",
      headers: {
        "X-Painel-User": viewer.user,
        "Content-Type": "application/json",
        "User-Agent": req.headers.get("user-agent") ?? "",
      },
      body: JSON.stringify(payload),
      cache: "no-store",
    });
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_fora" }, { status: 502 });
  }
}
