import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// POST /api/lead/nome  { lead_id, nome }  →  POST /painel/lead/{id}/nome
// Edita o nome do lead SÓ no nosso banco (nunca no Kommo). Escopo/validação é
// do copiloto via X-Painel-User: corretora só edita lead dela; admin qualquer.
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => null);
  if (!body || !body.lead_id || !body.nome) {
    return NextResponse.json({ error: "body_invalido" }, { status: 400 });
  }
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/lead/${encodeURIComponent(body.lead_id)}/nome`,
      {
        method: "POST",
        headers: {
          "X-Painel-User": viewer.user,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ nome: body.nome }),
        cache: "no-store",
      },
    );
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
