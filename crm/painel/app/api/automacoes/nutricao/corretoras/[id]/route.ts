import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// PUT /api/automacoes/nutricao/corretoras/{id}  { ligada: bool }
//
// Liga/desliga a nutrição dos leads de UMA corretora (a dona do lead; a linha de saída é a da Pessoa12 pra todas).
// Grava só o item dela no mapa, com autor no histórico, e devolve `gravado`
// RELIDO do banco. O geral (`nutricao_ativa`) desligado continua vencendo. Não
// envia nada.

type Ctx = { params: { id: string } };

export async function PUT(req: NextRequest, { params }: Ctx) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  if (!viewer.isAdmin) {
    return NextResponse.json({ error: "somente_gestao" }, { status: 403 });
  }
  if (!/^\d+$/.test(params.id)) {
    return NextResponse.json({ error: "corretora_invalida" }, { status: 400 });
  }
  const body = await req.json().catch(() => null);
  if (body === null || typeof body.ligada !== "boolean") {
    return NextResponse.json({ error: "ligada_obrigatorio" }, { status: 400 });
  }
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/automacoes/nutricao/corretoras/${params.id}`, {
      method: "PUT",
      headers: { "X-Painel-User": viewer.user, "Content-Type": "application/json" },
      body: JSON.stringify({ ligada: body.ligada }),
      cache: "no-store",
    });
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
