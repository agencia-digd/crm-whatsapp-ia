import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// POST /api/ligacoes/adiantar → copiloto /painel/ligacoes/adiantar
// "Quer adiantar a tarefa de amanhã?" — libera mais um bloco de ligações HOJE,
// só depois de fechar o bloco corrente. Não dobra nem zera o dia seguinte: o
// que anda é o progresso da SEMANA. Escopo por corretora via X-Painel-User.
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/ligacoes/adiantar`, {
      method: "POST",
      headers: { "X-Painel-User": viewer.user },
      cache: "no-store",
    });
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
