import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Foto de perfil (WhatsApp) do lead — hoje só pros leads da Pessoa12; o copiloto
// consulta o Evolution do Mac e cacheia 24h. Retorna {url|null}.
export async function GET(req: NextRequest) {
  const leadId = req.nextUrl.searchParams.get("lead_id");
  if (!leadId) {
    return NextResponse.json({ error: "lead_id_obrigatorio" }, { status: 400 });
  }
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/conversa/foto?lead_id=${encodeURIComponent(leadId)}`,
      { headers: { "X-Painel-User": viewer.user }, cache: "no-store" },
    );
    const body = await r.json().catch(() => ({ url: null }));
    return NextResponse.json(body, { status: r.ok ? 200 : r.status });
  } catch {
    return NextResponse.json({ url: null, error: "copiloto_indisponivel" }, { status: 502 });
  }
}
