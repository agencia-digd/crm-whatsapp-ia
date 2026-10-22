import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// GET /api/automacoes/boas-vindas/roleta
// PUT /api/automacoes/boas-vindas/roleta  { corretoras: [ids na ordem], confirmar_vazia?: bool }
//
// A roleta do Meta (quem recebe lead novo e em que ordem). Quem valida é o
// copiloto: só corretora ativa, sem repetir, e vazia só com `confirmar_vazia`
// (409 sem ela). A resposta traz o que ficou gravado RELIDO do banco; a tela
// confere com o que pediu. Não envia nada e não redistribui lead com dona.

async function repassar(req: NextRequest, metodo: "GET" | "PUT", body?: unknown) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  if (!viewer.isAdmin) {
    return NextResponse.json({ error: "somente_gestao" }, { status: 403 });
  }
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/automacoes/boas-vindas/roleta`, {
      method: metodo,
      headers: {
        "X-Painel-User": viewer.user,
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      cache: "no-store",
    });
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}

export async function GET(req: NextRequest) {
  return repassar(req, "GET");
}

export async function PUT(req: NextRequest) {
  const body = await req.json().catch(() => null);
  if (body === null || typeof body !== "object" || Array.isArray(body) || !Array.isArray(body.corretoras)) {
    return NextResponse.json({ error: "corretoras_obrigatorio" }, { status: 400 });
  }
  const envio: Record<string, unknown> = { corretoras: body.corretoras };
  if ("confirmar_vazia" in body) envio.confirmar_vazia = body.confirmar_vazia;
  return repassar(req, "PUT", envio);
}
