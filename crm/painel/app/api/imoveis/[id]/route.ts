import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Edição da captação — proxy pro copiloto.
// GET   /api/imoveis/{id}  →  GET /painel/imoveis/{id}  { imovel, fotos, eventos, … }
// PATCH /api/imoveis/{id} { campos alterados }  →  PATCH /painel/imoveis/{id}
//   200 { ok, alterado: [...], imovel }
// Escopo (corretora só edita a dela) e transferência (só admin) são decididos
// no copiloto, que também grava o histórico de quem mudou o quê.
export async function GET(
  req: NextRequest,
  { params }: { params: { id: string } },
) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/imoveis/${encodeURIComponent(params.id)}`,
      { headers: { "X-Painel-User": viewer.user }, cache: "no-store" },
    );
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}

export async function PATCH(
  req: NextRequest,
  { params }: { params: { id: string } },
) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => null);
  if (!body || typeof body !== "object") {
    return NextResponse.json({ error: "corpo_invalido" }, { status: 400 });
  }
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/imoveis/${encodeURIComponent(params.id)}`,
      {
        method: "PATCH",
        headers: {
          "X-Painel-User": viewer.user,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(body),
      },
    );
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
