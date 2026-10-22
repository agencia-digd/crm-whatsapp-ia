import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// GET /api/crm/lead-por-ref?external_ref=<kommo_lead_id>
//   → copiloto /painel/crm/lead-por-ref
// Resolve o vínculo do lead DIRETO na espinha canônica (leads), sem varrer os
// boards — o corte de cards por coluna escondia lead real e o seletor de etapa
// do Chat V2 sumia (bug 24/07). Resposta: {vinculado:true, lead_id, funil,
// etapa} ou {vinculado:false}. Escopo/privacidade é o copiloto quem resolve
// via X-Painel-User (corretora só resolve lead dela → 403 caso contrário).
export async function GET(req: NextRequest) {
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  const ref = (req.nextUrl.searchParams.get("external_ref") || "").trim();
  if (!ref) {
    return NextResponse.json({ error: "external_ref_obrigatorio" }, { status: 400 });
  }
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/crm/lead-por-ref?external_ref=${encodeURIComponent(ref)}`,
      { headers: { "X-Painel-User": viewer.user }, cache: "no-store" },
    );
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
