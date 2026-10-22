import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Notas internas da thread — anotações privadas da equipe ("liguei, não
// atendeu"). NUNCA vão pro lead: é INSERT puro no copiloto (tabela
// observacao_corretora, sem IA e sem nota no CRM). Privacidade é do copiloto
// (X-Painel-User): corretora só no lead dela; admin em qualquer.

// GET /api/conversa/notas?lead_id=X  →  GET /painel/lead/{id}/notas
export async function GET(req: NextRequest) {
  const leadId = req.nextUrl.searchParams.get("lead_id");
  if (!leadId) {
    return NextResponse.json({ error: "lead_id_obrigatorio" }, { status: 400 });
  }
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/lead/${encodeURIComponent(leadId)}/notas`,
      { headers: { "X-Painel-User": viewer.user }, cache: "no-store" },
    );
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}

// POST /api/conversa/notas  { lead_id, texto }  →  POST /painel/lead/{id}/nota
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => null);
  if (!body || !body.lead_id || !body.texto) {
    return NextResponse.json({ error: "body_invalido" }, { status: 400 });
  }
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/lead/${encodeURIComponent(body.lead_id)}/nota`,
      {
        method: "POST",
        headers: {
          "X-Painel-User": viewer.user,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ texto: body.texto }),
        cache: "no-store",
      },
    );
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
