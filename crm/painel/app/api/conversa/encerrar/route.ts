import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// POST /api/conversa/encerrar  { telefone }  →  POST /painel/conversa/encerrar
// "Feito" do Meu Dia: a conversa acabou com a mensagem do CLIENTE e não precisa
// de resposta. O copiloto grava o INSTANTE da última mensagem — se a pessoa
// escrever depois, a conversa volta sozinha pro bloco.
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => null);
  if (!body?.telefone) {
    return NextResponse.json({ error: "telefone_obrigatorio" }, { status: 400 });
  }
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/conversa/encerrar`, {
      method: "POST",
      headers: {
        "X-Painel-User": viewer.user,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ telefone: body.telefone }),
      cache: "no-store",
    });
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
