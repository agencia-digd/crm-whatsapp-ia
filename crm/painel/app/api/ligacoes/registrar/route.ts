import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// POST /api/ligacoes/registrar  { lead_id, resultado, observacao? }
//   →  POST /painel/ligacoes/registrar
// Registra o resultado de uma ligação da janela. Em "atendeu e quer continuar"
// o copiloto ADOTA o lead (etapa de entrada + dona + tag de remarketing) — com
// as travas de não rebaixar etapa e não tirar dono de quem já tem.
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => null);
  const leadId = body?.lead_id;
  const resultado = body?.resultado;
  if (leadId == null || Number.isNaN(Number(leadId))) {
    return NextResponse.json({ error: "lead_id_invalido" }, { status: 400 });
  }
  if (typeof resultado !== "string" || !resultado) {
    return NextResponse.json({ error: "resultado_obrigatorio" }, { status: 400 });
  }
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/ligacoes/registrar`, {
      method: "POST",
      headers: {
        "X-Painel-User": viewer.user,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        lead_id: Number(leadId),
        resultado,
        observacao: body?.observacao ?? null,
        corretora_id: body?.corretora_id ?? null,
      }),
      cache: "no-store",
    });
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
