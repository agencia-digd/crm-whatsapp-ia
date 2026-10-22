import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Foto da captação — proxy pro copiloto (GET /painel/imoveis/fotos/{id}).
//
// Existe como proxy, e não como link direto, pelo mesmo motivo que o copiloto
// serve a foto por endpoint com escopo: do outro lado tem telefone de dono e
// foto da casa de gente real. Caminho público exporia isso pra qualquer um com
// a URL.
//
// A resposta é binária: passa adiante o corpo e o content-type sem tocar.
export async function GET(
  req: NextRequest,
  { params }: { params: { id: string } },
) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const id = encodeURIComponent(params.id);
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/imoveis/fotos/${id}`, {
      headers: { "X-Painel-User": viewer.user },
      cache: "no-store",
    });
    if (!r.ok) {
      return NextResponse.json({ error: "foto_indisponivel" }, { status: r.status });
    }
    const buf = await r.arrayBuffer();
    return new NextResponse(buf, {
      status: 200,
      headers: {
        "Content-Type": r.headers.get("content-type") ?? "image/jpeg",
        // privada: é foto de imóvel de cliente, não asset público
        "Cache-Control": "private, max-age=300",
      },
    });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
