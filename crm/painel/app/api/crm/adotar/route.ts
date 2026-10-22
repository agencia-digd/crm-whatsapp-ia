import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// POST /api/crm/adotar  →  POST /painel/crm/adotar
// Body: { external_ref, etapa }. Adota pro funil principal um lead que existe
// no Kommo (leads_snapshot) mas ainda não tem card na espinha (`leads`): o
// copiloto cria o vínculo (reusa lead_spine.upsert_lead_shadow) e seta a etapa
// pelo mesmo caminho do mover manual (lead_events stage_manual_v2; cadência
// ligada se destino = Follow-up). Escopo/validação é o copiloto quem resolve
// via X-Painel-User (corretora só adota lead dela; etapa ∈ 8 canônicas).
export async function POST(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const body = await req.json().catch(() => null);
  if (!body || !body.external_ref || !body.etapa) {
    return NextResponse.json({ error: "body_invalido" }, { status: 400 });
  }
  try {
    const r = await fetch(`${cfg.copilotoUrl}/painel/crm/adotar`, {
      method: "POST",
      headers: {
        "X-Painel-User": viewer.user,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        external_ref: body.external_ref,
        etapa: body.etapa,
      }),
      cache: "no-store",
    });
    const out = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(out, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
