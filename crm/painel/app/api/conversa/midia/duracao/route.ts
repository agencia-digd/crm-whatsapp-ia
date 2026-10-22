import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Duração (segundos) de um áudio da conversa — o player mostra antes de tocar.
// O iPhone não lê nada do arquivo antes do play, por isso o servidor mede.
// Só leitura; a trava de privacidade é a mesma da mídia (copiloto).
export async function GET(req: NextRequest) {
  const leadId = req.nextUrl.searchParams.get("lead_id");
  const msgId = req.nextUrl.searchParams.get("msg_id");
  if (!leadId || !msgId) {
    return NextResponse.json({ error: "params_obrigatorios" }, { status: 400 });
  }
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/conversa/midia/duracao?lead_id=${encodeURIComponent(leadId)}&msg_id=${encodeURIComponent(msgId)}`,
      { headers: { "X-Painel-User": viewer.user }, cache: "no-store" },
    );
    const body = await r.json().catch(() => ({ error: "resposta_invalida" }));
    return NextResponse.json(body, {
      status: r.status,
      headers: r.ok ? { "Cache-Control": "private, max-age=86400" } : undefined,
    });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}
