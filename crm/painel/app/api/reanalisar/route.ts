import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Reanálise sob demanda do Inspector (botão 🔁 no raio-X do chat) via
// copiloto POST /painel/atendimento/{lead_id}/reanalisar. A análise roda
// LLM (30-90s) — timeout folgado de 120s.
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => ({}));
  const leadId = String(body.lead_id ?? "").trim();
  if (!leadId) return NextResponse.json({ erro: "lead_id_obrigatorio" }, { status: 422 });
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/atendimento/${encodeURIComponent(leadId)}/reanalisar`,
      {
        method: "POST",
        headers: { "X-Painel-User": viewer.user },
        cache: "no-store",
        signal: AbortSignal.timeout(180000),
      },
    );
    const data = await r.json().catch(() => ({ erro: "resposta_invalida" }));
    return NextResponse.json(data, { status: r.status });
  } catch {
    return NextResponse.json({ erro: "analise_falhou" }, { status: 502 });
  }
}
