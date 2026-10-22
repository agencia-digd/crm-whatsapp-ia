import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Marca "acertou / errou" num rascunho da IA via copiloto-api POST /aprendiz/acerto.
// O copiloto decide o escopo: corretora só marca as dela; admin marca qualquer uma.
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const corpo = await req.json().catch(() => null);
  if (!corpo || typeof corpo.decisao_id !== "number") {
    return NextResponse.json({ error: "decisao_id_obrigatorio" }, { status: 400 });
  }
  const acerto = corpo.acerto === true ? true : corpo.acerto === false ? false : null;
  try {
    const r = await fetch(`${cfg.copilotoUrl}/aprendiz/acerto`, {
      method: "POST",
      headers: { "X-Painel-User": viewer.user, "Content-Type": "application/json" },
      body: JSON.stringify({ decisao_id: corpo.decisao_id, acerto }),
      cache: "no-store",
    });
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
