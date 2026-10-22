import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// GET /api/lead?tel=... → copiloto /painel/lead/por-telefone
//
// Alimenta a tela que abre da notificação de lead novo. Best-effort: se
// falhar, a tela continua funcionando com nome e telefone da URL — que é o
// que a corretora precisa pra ligar. Por isso devolvemos {encontrado:false}
// em vez de erro quando o copiloto está fora.
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const tel = req.nextUrl.searchParams.get("tel") ?? "";
  if (!tel) {
    return NextResponse.json({ encontrado: false }, { status: 400 });
  }
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/lead/por-telefone?tel=${encodeURIComponent(tel)}`,
      { headers: { "X-Painel-User": viewer.user }, cache: "no-store" },
    );
    const body = await r.json().catch(() => ({ encontrado: false }));
    return NextResponse.json(body, { status: r.ok ? 200 : r.status });
  } catch {
    return NextResponse.json({ encontrado: false }, { status: 200 });
  }
}
