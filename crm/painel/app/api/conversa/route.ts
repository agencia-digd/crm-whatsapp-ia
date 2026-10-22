import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Thread WhatsApp do lead via copiloto-api (roteia pela linha da corretora
// dona — Cloud API pra Pessoa12, Evolution pras demais). Traz junto coaching +
// raio-X do Inspector. Privacidade no copiloto: corretora só lê lead dela
// (403 lead_de_outra_corretora). Somente leitura.
export async function GET(req: NextRequest) {
  const leadId = req.nextUrl.searchParams.get("lead_id");
  if (!leadId) {
    return NextResponse.json({ error: "lead_id_obrigatorio" }, { status: 400 });
  }
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/conversa?lead_id=${encodeURIComponent(leadId)}&hours_back=4320`,
      { headers: { "X-Painel-User": viewer.user }, cache: "no-store" },
    );
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, { status: r.status });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
