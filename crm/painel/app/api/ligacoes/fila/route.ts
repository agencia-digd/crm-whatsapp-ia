import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// GET /api/ligacoes/fila?limite=50 → copiloto /painel/ligacoes/fila
// Fila de quem ligar: leads do REMARKETING sem dono, com o contexto pra ligar
// (nome, telefone, última conversa) e quantas tentativas já houve. Escopo
// resolvido no copiloto via X-Painel-User.
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const limite = req.nextUrl.searchParams.get("limite") ?? "50";
  // ver_tudo = visão de SUPERVISÃO (admin): mostra o que está reservado com
  // cada corretora e não reserva nada. O copiloto recusa pra não-admin.
  const verTudo = req.nextUrl.searchParams.get("ver_tudo") === "1" ? "&ver_tudo=1" : "";
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/ligacoes/fila?limite=${encodeURIComponent(limite)}${verTudo}`,
      { headers: { "X-Painel-User": viewer.user }, cache: "no-store" },
    );
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
