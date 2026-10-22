import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Upload de foto da captação — proxy pro copiloto.
// POST /api/imoveis/{id}/fotos  (multipart: arquivo)  →  POST /painel/imoveis/{id}/fotos
//
// O corpo passa adiante como stream, sem o Next bufferizar e re-serializar:
// foto de celular chega com 3-8 MB e o dono está esperando do outro lado da
// conversa. `duplex: "half"` é exigido pelo fetch do Node quando o body é stream.
export async function POST(
  req: NextRequest,
  { params }: { params: { id: string } },
) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const ct = req.headers.get("content-type") || "";
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/imoveis/${encodeURIComponent(params.id)}/fotos`,
      {
        method: "POST",
        headers: { "X-Painel-User": viewer.user, "Content-Type": ct },
        body: req.body,
        // @ts-expect-error -- duplex existe no fetch do Node, não na tipagem DOM
        duplex: "half",
      },
    );
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
