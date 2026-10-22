import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Ordem das fotos — proxy pro copiloto. A primeira da lista vira a capa.
// PUT /api/imoveis/{id}/fotos/ordem { ordem: [foto_id, …] }
//   →  PUT /painel/imoveis/{id}/fotos/ordem   (409 = lista desatualizada)
export async function PUT(
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
      `${cfg.copilotoUrl}/painel/imoveis/${encodeURIComponent(params.id)}/fotos/ordem`,
      {
        method: "PUT",
        headers: { "X-Painel-User": viewer.user, "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
