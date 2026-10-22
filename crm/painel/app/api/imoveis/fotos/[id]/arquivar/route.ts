import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Excluir foto da captação — proxy pro copiloto. No banco é `arquivada=true`,
// nada se apaga (dá pra recuperar se a corretora excluir por engano).
// POST /api/imoveis/fotos/{id}/arquivar  →  POST /painel/imoveis/fotos/{id}/arquivar
export async function POST(
  req: NextRequest,
  { params }: { params: { id: string } },
) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/imoveis/fotos/${encodeURIComponent(params.id)}/arquivar`,
      { method: "POST", headers: { "X-Painel-User": viewer.user } },
    );
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
