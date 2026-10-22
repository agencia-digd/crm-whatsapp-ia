import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// PUT    /api/automacoes/copies/{toque}  { texto } → salva override da copy
// DELETE /api/automacoes/copies/{toque}            → restaura o padrão
// Só o TEXTO do toque muda — cronograma/guards/cap seguem no copiloto.
// Admin-only (copiloto revalida via X-Painel-User). toque -1 = remarketing.

type Ctx = { params: { toque: string } };

function toqueValido(raw: string): number | null {
  const n = Number(raw);
  return Number.isInteger(n) ? n : null;
}

export async function PUT(req: NextRequest, { params }: Ctx) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  if (!viewer.isAdmin) {
    return NextResponse.json({ error: "somente_gestao" }, { status: 403 });
  }
  const toque = toqueValido(params.toque);
  if (toque === null) {
    return NextResponse.json({ error: "toque_invalido" }, { status: 400 });
  }
  const body = await req.json().catch(() => null);
  if (typeof body?.texto !== "string") {
    return NextResponse.json({ error: "texto_obrigatorio" }, { status: 400 });
  }
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/automacoes/copies/${toque}`, {
      method: "PUT",
      headers: {
        "X-Painel-User": viewer.user,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ texto: body.texto }),
      cache: "no-store",
    });
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}

export async function DELETE(req: NextRequest, { params }: Ctx) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  if (!viewer.isAdmin) {
    return NextResponse.json({ error: "somente_gestao" }, { status: 403 });
  }
  const toque = toqueValido(params.toque);
  if (toque === null) {
    return NextResponse.json({ error: "toque_invalido" }, { status: 400 });
  }
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/automacoes/copies/${toque}`, {
      method: "DELETE",
      headers: { "X-Painel-User": viewer.user },
      cache: "no-store",
    });
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
