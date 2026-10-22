import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Cadência Pessoa12 (P2) via copiloto-api — visão em tempo real dos 8 toques D0-D14
// + remarketing. Admin-only (o copiloto valida via X-Painel-User).
// ?status= ativa | concluida | remarketing | opt_out ; ?page= ; ?page_size=
export async function GET(req: NextRequest) {
  const sp = req.nextUrl.searchParams;
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  if (!viewer.isAdmin) {
    return NextResponse.json({ error: "somente_gestao" }, { status: 403 });
  }

  const qs = new URLSearchParams();
  if (sp.get("status")) qs.set("status", sp.get("status")!);
  if (sp.get("page")) qs.set("page", sp.get("page")!);
  if (sp.get("page_size")) qs.set("page_size", sp.get("page_size")!);

  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/cadencia?${qs.toString()}`, {
      headers: { "X-Painel-User": viewer.user },
      cache: "no-store",
    });
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
