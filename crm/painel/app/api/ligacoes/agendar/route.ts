import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// POST /api/ligacoes/agendar  { lead_id, data, hora, observacao? }
//   →  POST /painel/ligacoes/agendar
// "Ligar mais tarde": a pessoa atendeu e pediu outro horário. Cria a tarefa
// "Ligar novamente" (a mesma tabela do chat, então ela aparece no Hoje) e NÃO
// registra resultado — o lead segue na fila, agora no topo e na hora marcada.
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => null);
  const leadId = body?.lead_id;
  if (leadId == null || Number.isNaN(Number(leadId))) {
    return NextResponse.json({ error: "lead_id_invalido" }, { status: 400 });
  }
  if (!body?.data || !body?.hora) {
    return NextResponse.json({ error: "data_e_hora_obrigatorias" }, { status: 400 });
  }
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/ligacoes/agendar`, {
      method: "POST",
      headers: {
        "X-Painel-User": viewer.user,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        lead_id: Number(leadId),
        data: body.data,
        hora: body.hora,
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
