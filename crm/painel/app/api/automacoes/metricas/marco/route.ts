import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// POST /api/automacoes/metricas/marco { motivo } → recomeça a MEDIÇÃO.
//
// Não apaga histórico: grava a data a partir da qual a métrica passa a contar.
// `cadencia_eventos` continua inteiro (é auditoria — é como a gente prova o que
// saiu pra quem). Admin-only aqui e revalidado no copiloto.
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  if (!viewer.isAdmin) {
    return NextResponse.json({ error: "somente_gestao" }, { status: 403 });
  }
  const body = await req.json().catch(() => null);
  const motivo = typeof body?.motivo === "string" ? body.motivo.trim() : "";
  if (!motivo) {
    return NextResponse.json({ error: "motivo_obrigatorio" }, { status: 400 });
  }
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/automacoes/metricas/marco`, {
      method: "POST",
      headers: {
        "X-Painel-User": viewer.user,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ motivo }),
      cache: "no-store",
    });
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
